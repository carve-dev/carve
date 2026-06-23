"""The dbt verification runner — bridge ``parse_dbt_run`` into the harness loop.

A dbt component is *executed* by running ``dbt build``/``dbt test`` against the
user's project; in Carve that execution rides the gated ``bash`` tool through
whatever dbt-execution backend the component uses. Wiring that live execution
into this loop is deferred to the **dbt-execution** unit — this module is the
parse → :class:`~carve.core.agents.verification.CheckResult` bridge plus the
verification-loop composition, nothing more (exactly as
``integrations/dlt/runner.py`` defers its live venv-runner wiring).

The substrate :func:`carve.integrations.dbt.verify.parse_dbt_run` *parses* a
finished dbt run (executed via the gated bash tool) into a
:class:`~carve.core.agents.verification.CheckResult` by reading the on-disk
``run_results.json`` — but its signature takes ``run_results_path`` +
``manifest_path`` keyword arguments, so it does **not** match the harness
``ParseFn`` contract (``CompletedProcess -> CheckResult``) that
:func:`carve.core.agents.verification.run_check` injects.

This module is the missing connective tissue: a closure that binds those artifact
paths into a ``ParseFn`` (:func:`make_dbt_parse_fn`), plus thin wrappers
(:func:`run_dbt_check`, :func:`make_dbt_verification_loop`) that compose the
harness ``run_check`` / :class:`VerificationLoop` with the dbt parser so the
agent — and, later, recovery — can bridge a finished dbt run into a real
:class:`CheckResult` without re-implementing the bridge.

**Single execution path (hard invariant).** This module opens **no** new
bash/exec path. Every command runs through the injected, gated ``bash`` tool
exactly as :func:`run_check` does — the gate's allowlist, scrubbed env, cwd-pin,
and output cap apply unchanged. The runner only *parses*; it never spawns a
subprocess of its own. The live ``dbt build``/``dbt test --select`` command this
loop would run is gated on dbt-execution; until then the verdict's truth comes
from the on-disk ``run_results.json`` the run wrote, not from the exit code — a
clean exit code alone is not trusted.

**Substrate caveat (already handled — confirmed, not re-handled here):**
``run_check`` routes all captured output to ``stdout`` with ``stderr=""``, and
``parse_dbt_run`` reads ``proc.stdout or proc.stderr`` — so the closure composes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from carve.core.agents.tools import Tool
from carve.core.agents.verification import (
    MAX_VERIFICATION_ITERATIONS,
    CheckResult,
    ParseFn,
    VerificationLoop,
    run_check,
)
from carve.integrations.dbt.verify import parse_dbt_run


def make_dbt_parse_fn(*, run_results_path: Path, manifest_path: Path | None = None) -> ParseFn:
    """Bind ``parse_dbt_run`` into the harness ``ParseFn`` contract.

    ``parse_dbt_run`` needs ``run_results_path`` (and optionally ``manifest_path``)
    to find the on-disk run-results artifact; ``run_check`` injects a ``ParseFn``
    that takes only a finished ``CompletedProcess``. This closes over the artifact
    paths and returns the ``ParseFn`` the harness expects — the whole bridge, in
    one place.
    """
    resolved_results = Path(run_results_path)
    resolved_manifest = Path(manifest_path) if manifest_path is not None else None

    def _parse(proc: subprocess.CompletedProcess[str]) -> CheckResult:
        return parse_dbt_run(
            proc,
            run_results_path=resolved_results,
            manifest_path=resolved_manifest,
        )

    return _parse


def run_dbt_check(
    cmd: str,
    *,
    run_results_path: Path,
    manifest_path: Path | None = None,
    bash_tool: Tool,
    timeout: int = 300,
) -> CheckResult:
    """Run ``cmd`` through the gated ``bash`` tool and bridge a finished dbt run.

    A one-call convenience over :func:`carve.core.agents.verification.run_check`
    with the dbt parse-fn already bound. The dbt run itself executes through the
    gated bash tool (the live ``dbt build``/``test`` wiring is deferred to
    dbt-execution); the verdict's truth comes from the on-disk
    ``run_results.json`` at ``run_results_path`` that ``parse_dbt_run`` reads —
    not from ``cmd``'s exit code alone.
    """
    return run_check(
        cmd,
        parse=make_dbt_parse_fn(run_results_path=run_results_path, manifest_path=manifest_path),
        bash_tool=bash_tool,
        timeout=timeout,
    )


def make_dbt_verification_loop(
    cmd: str,
    *,
    run_results_path: Path,
    manifest_path: Path | None = None,
    bash_tool: Tool,
    max_iterations: int = MAX_VERIFICATION_ITERATIONS,
    timeout: int = 300,
) -> VerificationLoop:
    """Build a :class:`VerificationLoop` wired to the dbt parse-fn.

    The agent rides this loop to bridge a finished dbt run (executed through the
    gated bash tool — the live ``dbt build``/``test`` wiring deferred to
    dbt-execution) into the parsed :class:`CheckResult`, and self-correct
    (author → run → read → fix) bounded by the loop's ceilings (``max_iterations``
    + the per-invocation cost budget). No new execution path: the loop runs
    ``cmd`` through ``bash_tool``, the same gated bash the agent uses.
    """
    return VerificationLoop(
        cmd,
        parse=make_dbt_parse_fn(run_results_path=run_results_path, manifest_path=manifest_path),
        bash_tool=bash_tool,
        max_iterations=max_iterations,
        timeout=timeout,
    )


__all__ = [
    "make_dbt_parse_fn",
    "make_dbt_verification_loop",
    "run_dbt_check",
]
