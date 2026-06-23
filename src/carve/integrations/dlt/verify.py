"""Parse a ``dlt pipeline run`` into a verification :class:`CheckResult`.

The harness verification loop (:func:`carve.core.agents.verification.run_check`)
runs a command through the gated ``bash`` tool and hands the finished process to
an *injected* parser. This is that parser for dlt: it combines the run's exit
status with dlt's **on-disk load package** — the persisted record of what the
run loaded, which schema changes it applied, and whether any job failed — into
a :class:`CheckResult` the agent grounds its fix loop on.

Why read the load package rather than trust the exit code alone: dlt collects
failed jobs into the package (``failed_jobs/``), and a script that doesn't call
``raise_on_failed_jobs()`` can still exit 0 with a partial load. Reading the
package is the truth. (Per-table *row counts* are not in the persisted package —
they live in the in-process trace — so they're left to a runner that emits them;
this parser reports tables, schema changes, and failures.)
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from carve.core.agents.verification import CheckResult

# dlt's internal bookkeeping tables — filtered from the user-facing summary.
_INTERNAL_PREFIX = "_dlt"

# Load-package lifecycle dirs under <pipelines_dir>/<name>/load/, newest wins.
# A package in ``loaded`` completed; one stuck in ``normalized``/``new`` did not.
_PACKAGE_STATES = ("loaded", "normalized", "new")


@dataclass(frozen=True)
class LoadPackageReport:
    """What dlt's persisted load package says about the last run."""

    load_id: str
    completed: bool  # the package reached the ``loaded`` state
    failed_jobs: tuple[str, ...]  # job ids that failed (empty on a clean load)
    schema_changes: tuple[str, ...]  # user tables whose schema was applied/changed
    tables: tuple[str, ...]  # user tables with a completed load job


def _read_json(path: Path) -> object | None:
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed


def _user_tables(names: object) -> tuple[str, ...]:
    if not isinstance(names, (list, tuple, set, dict)):
        return ()
    return tuple(
        sorted({n for n in names if isinstance(n, str) and not n.startswith(_INTERNAL_PREFIX)})
    )


def read_latest_load_package(pipelines_dir: Path, pipeline_name: str) -> LoadPackageReport | None:
    """Read the newest load package for ``pipeline_name``, or None if absent."""
    base = Path(pipelines_dir) / pipeline_name / "load"
    candidates: list[tuple[str, str, Path]] = []
    for state in _PACKAGE_STATES:
        state_dir = base / state
        if state_dir.is_dir():
            candidates += [(p.name, state, p) for p in state_dir.iterdir() if p.is_dir()]
    if not candidates:
        return None
    # dlt load ids are str(unix_time) — order numerically (newest wins), with a
    # string tie-break so a non-numeric id (shouldn't happen) still sorts safely.
    load_id, state, pkg = max(candidates, key=lambda c: (_as_float(c[0]), c[0]))

    failed_dir = pkg / "failed_jobs"
    failed = tuple(sorted(p.name for p in failed_dir.iterdir())) if failed_dir.is_dir() else ()

    schema_updates = _read_json(pkg / "applied_schema_updates.json")
    schema_changes = _user_tables(schema_updates) if isinstance(schema_updates, dict) else ()

    return LoadPackageReport(
        load_id=load_id,
        completed=(state == "loaded"),
        failed_jobs=failed,
        schema_changes=schema_changes,
        tables=_completed_tables(pkg),
    )


def _completed_tables(pkg: Path) -> tuple[str, ...]:
    """User tables with a completed job — from load_metrics, else job filenames."""
    state = _read_json(pkg / "load_package_state.json")
    metrics = state.get("load_metrics") if isinstance(state, dict) else None
    if isinstance(metrics, dict):
        names = [
            m["table_name"]
            for m in metrics.values()
            if isinstance(m, dict) and m.get("state") == "completed" and "table_name" in m
        ]
        return _user_tables(names)
    completed = pkg / "completed_jobs"
    if completed.is_dir():
        # filenames look like "<table>.<hash>.<n>.<format>.gz"
        return _user_tables([p.name.split(".", 1)[0] for p in completed.iterdir()])
    return ()


def parse_dlt_run(
    proc: subprocess.CompletedProcess[str],
    *,
    pipelines_dir: Path | None = None,
    pipeline_name: str | None = None,
) -> CheckResult:
    """Map a finished ``dlt pipeline run`` to a :class:`CheckResult`.

    A non-zero exit is a failure (the agent reads the error tail). On a clean
    exit, the on-disk load package is the source of truth: the run passed only
    if its newest package reached ``loaded`` with no failed jobs.
    """
    if proc.returncode != 0:
        # `run_check` routes all captured output to stdout (stderr is ""), so
        # read the error from there; surface the actual error line (usually at
        # the end of a dlt traceback), not the first log preamble line.
        output = proc.stdout or proc.stderr or ""
        return CheckResult(
            passed=False,
            summary=_error_summary(output) or f"dlt run exited {proc.returncode}.",
            details={"returncode": proc.returncode, "output_tail": _tail(output)},
        )

    report = None
    if pipelines_dir is not None and pipeline_name is not None:
        report = read_latest_load_package(pipelines_dir, pipeline_name)
    if report is None:
        # Exit 0 but nothing to introspect (no pipelines_dir, or no package
        # written) — trust the exit code, and say so plainly.
        return CheckResult(passed=True, summary="dlt run exited 0.", details={"returncode": 0})

    passed = report.completed and not report.failed_jobs
    if passed:
        listed = f" ({', '.join(report.tables)})" if report.tables else ""
        summary = (
            f"Loaded {len(report.tables)} table(s){listed}; "
            f"{len(report.schema_changes)} schema change(s)."
        )
    else:
        reason = (
            f"{len(report.failed_jobs)} failed job(s)"
            if report.failed_jobs
            else f"load package {report.load_id} did not reach 'loaded'"
        )
        summary = f"dlt load did not complete cleanly: {reason}."
    return CheckResult(
        passed=passed,
        summary=summary,
        details={
            "load_id": report.load_id,
            "completed": report.completed,
            "tables": list(report.tables),
            "schema_changes": list(report.schema_changes),
            "failed_jobs": list(report.failed_jobs),
        },
    )


_ERROR_MARKERS = ("Error", "Exception", "Failed", "Traceback")


def _error_summary(output: str) -> str:
    """The most informative error line — the last marker line, else the last line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if any(marker in line for marker in _ERROR_MARKERS):
            return line[:300]
    return lines[-1][:300] if lines else ""


def _as_float(load_id: str) -> float:
    try:
        return float(load_id)
    except ValueError:
        return float("-inf")


def _tail(text: str | None, *, limit: int = 1500) -> str:
    return (text or "")[-limit:]


__all__ = ["LoadPackageReport", "parse_dlt_run", "read_latest_load_package"]
