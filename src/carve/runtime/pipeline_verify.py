"""The pipeline-engineer verify-by-validate vertical.

The pipeline engineer closes the loop on its own composition rather than
handing the orchestrator an unverified TOML: after writing/editing a
``pipelines/<name>.toml`` it runs ``carve pipelines validate <name>`` and, if
the structured validation fails (a cycle, a dangling ``depends_on``, an
unresolvable component name), reads the error and self-corrects until green —
bounded by the harness's attempt cap.

This module is the connective tissue between ``carve pipelines validate`` and
the harness verification loop, mirroring ``integrations/dlt/runner.py``:

* :func:`parse_pipeline_validate` adapts a finished ``carve pipelines
  validate`` :class:`subprocess.CompletedProcess` into a harness
  :class:`~carve.core.agents.verification.CheckResult` — ``passed`` from the
  exit code (the command exits non-zero on any :class:`PipelineError`),
  ``summary``/``details`` from the rendered error text on stdout.
* :func:`make_pipeline_validate_parse_fn` exposes that as a ready
  :class:`~carve.core.agents.verification.ParseFn` for callers that compose
  ``run_check`` themselves.
* :func:`pipeline_validate_command` centralizes the validate command shape so
  callers don't hand-build ``carve pipelines validate`` strings (the harness
  ``bash`` gate denies command chaining, so it stays a single command).
* :func:`run_pipeline_validate_check` / :func:`make_pipeline_verification_loop`
  compose the harness ``run_check`` / :class:`VerificationLoop` with the
  parse-fn so the agent — and, later, recovery — can verify a composition
  without re-implementing the bridge.
* :func:`run_pipeline_dev_run` is the optional dev-run path: it drives the
  **shipped** :func:`carve.runtime.execute_pipeline.execute_pipeline` over a
  registry built by :func:`carve.runtime.step_types.registry.build_step_executor_registry`
  (the creds-free DuckDB substrate), returning a :class:`CheckResult` from the
  run's derived status — so the engineer can do a single dev execution when
  the task warrants.

**Single execution path (hard invariant).** The validate check runs through
the injected, gated ``bash`` tool exactly as :func:`run_check` does — the
gate's allowlist, scrubbed env, cwd-pin, and output cap apply unchanged. This
module opens no new bash/exec path; it only *parses* the validate result and
*drives* the in-process dev DAG walk.

**Deferred (by design).** The LIVE orchestrator goal-routing that delegates a
composition goal to the pipeline engineer and rides this loop is cross-cutting
plan-build-classifier wiring — it stays unbuilt here. The loop is testable in
isolation with a stub/injected delegate (the fix step), which is what the
``tests/runtime/test_pipeline_verify.py`` suite exercises.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from carve.core.agents.tools import Tool
from carve.core.agents.verification import (
    MAX_VERIFICATION_ITERATIONS,
    CheckResult,
    ParseFn,
    VerificationLoop,
    run_check,
)
from carve.runtime.execute_pipeline import RunResult, execute_pipeline
from carve.runtime.step_types.registry import build_step_executor_registry

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.schema import ComponentConfig, ConnectionsConfig
    from carve.runtime.run_context import PipelineRun

# The trailing slice of validate output threaded into details on a failure —
# the rendered PipelineError (message/file/field/hint) is already compact, but
# cap it so a runaway output never bloats the agent's context.
_OUTPUT_TAIL_LIMIT = 2000


def pipeline_validate_command(pipeline_name: str | None = None) -> str:
    """Build the ``carve pipelines validate`` command.

    With a ``pipeline_name`` it validates that one pipeline; without, it
    validates every ``pipelines/*.toml``. This centralizes the command shape so
    callers don't hand-build ``carve pipelines validate`` strings — the harness
    ``bash`` gate denies command chaining (``&&``/``;``/pipes), so this stays a
    single gate-shaped command.
    """
    if pipeline_name is None:
        return "carve pipelines validate"
    return f"carve pipelines validate {pipeline_name}"


def parse_pipeline_validate(proc: subprocess.CompletedProcess[str]) -> CheckResult:
    """Adapt a finished ``carve pipelines validate`` run into a :class:`CheckResult`.

    ``carve pipelines validate`` exits ``0`` when every pipeline loads cleanly
    and non-zero on the first :class:`PipelineError` (rendering its
    ``message``/``file``/``field``/``hint`` to stdout). So ``passed`` is the
    exit code being ``0``; the rendered output is the ``summary`` (first line)
    and the ``details`` diagnostic tail. ``run_check`` routes all captured
    output to ``stdout`` with ``stderr=""``, so reading ``proc.stdout`` (falling
    back to ``stderr``) composes with it.
    """
    output = (proc.stdout or proc.stderr or "").strip()
    passed = proc.returncode == 0
    if passed:
        summary = _first_line(output) or "carve pipelines validate passed."
        return CheckResult(
            passed=True,
            summary=summary,
            details={"exit_code": proc.returncode},
        )
    summary = _first_line(output) or "carve pipelines validate failed."
    return CheckResult(
        passed=False,
        summary=summary[:300],
        details={
            "exit_code": proc.returncode,
            "output_tail": output[-_OUTPUT_TAIL_LIMIT:],
        },
    )


def make_pipeline_validate_parse_fn() -> ParseFn:
    """Return the :class:`ParseFn` that bridges ``carve pipelines validate``.

    The harness ``run_check`` injects a ``ParseFn`` taking only a finished
    ``CompletedProcess``; :func:`parse_pipeline_validate` already matches that
    contract, so this is the named export callers reach for.
    """
    return parse_pipeline_validate


def run_pipeline_validate_check(
    *,
    bash_tool: Tool,
    pipeline_name: str | None = None,
    timeout: int = 120,
) -> CheckResult:
    """Run ``carve pipelines validate`` through the gated ``bash`` tool, parsed.

    A one-call convenience over :func:`carve.core.agents.verification.run_check`
    with the pipeline parse-fn already bound. No new execution path: the command
    runs through ``bash_tool``, the same gated bash the agent uses.
    """
    return run_check(
        pipeline_validate_command(pipeline_name),
        parse=parse_pipeline_validate,
        bash_tool=bash_tool,
        timeout=timeout,
    )


def make_pipeline_verification_loop(
    *,
    bash_tool: Tool,
    pipeline_name: str | None = None,
    max_iterations: int = MAX_VERIFICATION_ITERATIONS,
    timeout: int = 120,
) -> VerificationLoop:
    """Build a :class:`VerificationLoop` wired to ``carve pipelines validate``.

    The engineer rides this loop to compose → validate → read the structured
    error → self-correct, bounded by the loop's ceiling (``max_iterations`` +
    the per-invocation cost budget). No new execution path: the loop runs the
    validate command through ``bash_tool``, the same gated bash the agent uses.
    """
    return VerificationLoop(
        pipeline_validate_command(pipeline_name),
        parse=parse_pipeline_validate,
        bash_tool=bash_tool,
        max_iterations=max_iterations,
        timeout=timeout,
    )


async def run_pipeline_dev_run(
    run: PipelineRun,
    *,
    paths: ProjectPaths,
    connections: ConnectionsConfig,
    dbt_executable: str,
    components: dict[str, ComponentConfig] | None = None,
) -> CheckResult:
    """Optional dev run: execute the pipeline over the creds-free substrate.

    Builds a :class:`StepExecutorRegistry` via the **shipped**
    :func:`build_step_executor_registry` (DuckDB-default connection factory, the
    injected dlt/dbt seams) and drives the **shipped** :func:`execute_pipeline`
    over the dev target. Returns a :class:`CheckResult` from the run's derived
    status (``succeeded`` -> passed; ``failed``/``partial`` -> not passed, with
    the failed/skipped partition in ``details``) so the engineer can fold a dev
    run into the same verify loop. The *live* orchestrator wiring that supplies
    real warehouse seams is deferred; this is the in-process dev path the
    engineer's optional dev run uses.
    """
    registry = build_step_executor_registry(
        connections=connections,
        dbt_executable=dbt_executable,
        components=components or {},
    )
    result = await execute_pipeline(
        run,
        paths=paths,
        registry=registry,
        components=components or {},
    )
    return _dev_run_to_check_result(result)


def _dev_run_to_check_result(result: RunResult) -> CheckResult:
    """Adapt a dev :class:`RunResult` into a harness :class:`CheckResult`."""
    passed = result.status == "succeeded"
    if passed:
        summary = f"dev run succeeded: {len(result.completed)} step(s) completed."
    else:
        summary = (
            f"dev run {result.status}: {len(result.failed)} failed, {len(result.skipped)} skipped."
        )
    return CheckResult(
        passed=passed,
        summary=summary,
        details={
            "status": result.status,
            "completed": sorted(result.completed),
            "failed": sorted(result.failed),
            "skipped": sorted(result.skipped),
            "warnings": list(result.warnings),
        },
    )


def _first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text else ""


__all__ = [
    "make_pipeline_validate_parse_fn",
    "make_pipeline_verification_loop",
    "parse_pipeline_validate",
    "pipeline_validate_command",
    "run_pipeline_dev_run",
    "run_pipeline_validate_check",
]
