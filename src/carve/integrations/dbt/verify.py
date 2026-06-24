"""Parse a finished ``dbt build``/``dbt test`` into a verification :class:`CheckResult`.

The harness verification loop (:func:`carve.core.agents.verification.run_check`)
runs a command through the gated ``bash`` tool and hands the finished process to
an *injected* parser. This is that parser for dbt: it combines the run's exit
status with dbt's **on-disk run-results artifact** (``target/run_results.json`` —
the persisted, per-node record of what passed/failed and why) into a
:class:`CheckResult` the agent grounds its fix loop on.

Why read ``run_results.json`` rather than trust the exit code alone: this is the
same invariant as the dlt parser (:func:`carve.integrations.dlt.verify.parse_dlt_run`).
A clean exit alone is **not** trusted — the on-disk artifact is the source of
truth. (In practice ``dbt build`` exits non-zero on a failed test, but a run can
be invoked with ``--warn-error``/flags that change exit semantics, and a partial
run still writes per-node statuses; the artifact is the truth.)

``run_results.json`` shape (the fields read here)::

    {
      "results": [
        {"unique_id": "model.pkg.stg_orders", "status": "success",
         "message": null, "failures": null, ...},
        {"unique_id": "test.pkg.not_null_orders_id", "status": "fail",
         "message": "Got 3 results, configured to fail if != 0", "failures": 3, ...}
      ],
      "args": {...}, "metadata": {...}
    }

dbt's per-node ``status`` is ``success``/``error``/``skipped`` for build nodes and
``pass``/``fail`` for tests (``runtime error`` also occurs). The run **passed** iff
every node is ``success``/``pass``/``skipped`` with no ``fail``/``error``/runtime
error. On failure the failing node id(s) + dbt's ``message`` are surfaced; readable
names are resolved via ``manifest.json`` when a ``manifest_path`` is supplied (the
dlt precedent's manifest-assist read).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from carve.core.agents.verification import CheckResult

# dbt per-node statuses that count as a clean / non-failing outcome.
_OK_STATUSES = frozenset({"success", "pass", "skipped"})

_ERROR_MARKERS = ("Error", "Exception", "Failed", "Failure", "Compilation Error")


@dataclass(frozen=True)
class DbtNodeResult:
    """One node's persisted result from ``run_results.json``.

    The structured per-node record the dbt-execution backend normalizes into a
    ``PerModelResult``: dbt's raw per-node ``status`` (``success``/``error``/
    ``skipped`` for build nodes, ``pass``/``fail`` for tests), the ``message``,
    and the ``failures`` count tests report. Carries the readable ``name``
    (manifest-resolved when a manifest is supplied, else the ``unique_id``).
    """

    unique_id: str
    name: str
    status: str
    message: str | None
    failures: int | None


@dataclass(frozen=True)
class DbtRunReport:
    """What dbt's persisted ``run_results.json`` says about the last run."""

    completed: bool  # the artifact was present and parseable
    passed_nodes: tuple[str, ...]  # node ids that succeeded/passed/were skipped
    failed_nodes: tuple[tuple[str, str], ...]  # (unique_id, message) for fail/error nodes
    # Every node's structured result, in file order — the backend-uniform
    # normalization layer (carve.core.dbt_execution) reads this to build its
    # per-model result list with raw status + failures count. The pass/fail
    # *verdict* still rides `passed_nodes`/`failed_nodes`; this is the detail
    # those buckets discard. Defaulted so existing constructors stay valid.
    nodes: tuple[DbtNodeResult, ...] = ()


def _read_json(path: Path) -> object | None:
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed


def _node_name(manifest: dict[str, object] | None, unique_id: str) -> str:
    """Resolve ``unique_id`` to a readable node name via the manifest, else the id."""
    if isinstance(manifest, dict):
        nodes = manifest.get("nodes")
        if isinstance(nodes, dict):
            node = nodes.get(unique_id)
            if isinstance(node, dict):
                name = node.get("name")
                if isinstance(name, str) and name:
                    return name
    return unique_id


def read_run_results(
    run_results_path: Path, *, manifest_path: Path | None = None
) -> DbtRunReport | None:
    """Read ``run_results.json`` into a :class:`DbtRunReport`, or None if absent/malformed.

    Fail-closed: a missing or malformed artifact returns ``None`` (the caller then
    treats the run as not-verified rather than silently green).
    """
    parsed = _read_json(run_results_path)
    if not isinstance(parsed, dict):
        return None
    results = parsed.get("results")
    if not isinstance(results, list):
        return None

    manifest = _read_json(manifest_path) if manifest_path is not None else None
    manifest_dict = manifest if isinstance(manifest, dict) else None

    passed: list[str] = []
    failed: list[tuple[str, str]] = []
    nodes: list[DbtNodeResult] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        unique_id = result.get("unique_id")
        if not isinstance(unique_id, str):
            continue
        status = str(result.get("status", "")).lower()
        name = _node_name(manifest_dict, unique_id)
        raw_failures = result.get("failures")
        failures = raw_failures if isinstance(raw_failures, int) else None
        raw_message = result.get("message")
        node_message = raw_message if isinstance(raw_message, str) and raw_message else None
        nodes.append(
            DbtNodeResult(
                unique_id=unique_id,
                name=name,
                status=status or "unknown",
                message=node_message,
                failures=failures,
            )
        )
        if status in _OK_STATUSES:
            passed.append(name)
        else:
            message = node_message or f"status={status or 'unknown'}"
            failed.append((name, message))

    return DbtRunReport(
        completed=True,
        passed_nodes=tuple(passed),
        failed_nodes=tuple(failed),
        nodes=tuple(nodes),
    )


def parse_dbt_run(
    proc: subprocess.CompletedProcess[str],
    *,
    run_results_path: Path | None = None,
    manifest_path: Path | None = None,
) -> CheckResult:
    """Map a finished ``dbt build``/``dbt test`` to a :class:`CheckResult`.

    A non-zero exit is a failure (the agent reads the error tail from
    ``proc.stdout or proc.stderr`` — ``run_check`` routes all output to stdout).
    On a clean exit, the on-disk ``run_results.json`` is the source of truth: the
    run passed only if every node succeeded/passed/was skipped with no fail/error.
    A clean exit with no readable artifact is **not** trusted as green — it is
    surfaced as needs-attention, mirroring dlt's "the artifact is the truth".
    """
    if proc.returncode != 0:
        output = proc.stdout or proc.stderr or ""
        report = (
            read_run_results(run_results_path, manifest_path=manifest_path)
            if run_results_path is not None
            else None
        )
        # Prefer dbt's structured per-node failure over the raw log tail.
        if report is not None and report.failed_nodes:
            return _failure_result(report, returncode=proc.returncode)
        return CheckResult(
            passed=False,
            summary=_error_summary(output) or f"dbt run exited {proc.returncode}.",
            details={"returncode": proc.returncode, "output_tail": _tail(output)},
        )

    if run_results_path is None:
        return CheckResult(passed=True, summary="dbt run exited 0.", details={"returncode": 0})

    report = read_run_results(run_results_path, manifest_path=manifest_path)
    if report is None:
        # Exit 0 but no readable run_results.json — the artifact is the truth, and
        # there isn't one; do not trust the bare exit code.
        return CheckResult(
            passed=False,
            summary="dbt run exited 0 but wrote no readable run_results.json.",
            details={"returncode": 0, "run_results_path": str(run_results_path)},
        )

    if report.failed_nodes:
        return _failure_result(report, returncode=proc.returncode)

    summary = f"dbt run passed: {len(report.passed_nodes)} node(s) succeeded."
    return CheckResult(
        passed=True,
        summary=summary,
        details={
            "returncode": proc.returncode,
            "passed_nodes": list(report.passed_nodes),
            "failed_nodes": [],
        },
    )


def _failure_result(report: DbtRunReport, *, returncode: int) -> CheckResult:
    failed = report.failed_nodes
    first_id, first_msg = failed[0]
    suffix = f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""
    summary = f"dbt run failed: {first_id} — {first_msg}{suffix}"
    return CheckResult(
        passed=False,
        summary=summary[:300],
        details={
            "returncode": returncode,
            "passed_nodes": list(report.passed_nodes),
            "failed_nodes": [{"node": node_id, "message": msg} for node_id, msg in failed],
        },
    )


def _error_summary(output: str) -> str:
    """The most informative error line — the last marker line, else the last line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if any(marker in line for marker in _ERROR_MARKERS):
            return line[:300]
    return lines[-1][:300] if lines else ""


def _tail(text: str | None, *, limit: int = 1500) -> str:
    return (text or "")[-limit:]


__all__ = ["DbtNodeResult", "DbtRunReport", "parse_dbt_run", "read_run_results"]
