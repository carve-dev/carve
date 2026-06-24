"""The ``dbt`` step executor — run a dbt component via its backend.

``DbtStepExecutor`` implements the Unit-1 :class:`StepExecutor` seam for a
``dbt`` step. It resolves the step's ``component`` (omitted → the single
detected dbt project), constructs the component's :class:`DbtBackend` (``local``
in this slice, the engine binary **injected**), builds a typed
:class:`DbtCommand`, and maps the backend-uniform :class:`DbtRunResult` to a
pipelines :class:`StepResult`.

Backend-agnostic by construction
--------------------------------
The executor never branches on which backend it holds: it calls
``backend.run(command)`` and normalizes the *same* :class:`DbtRunResult`
whatever produced it (the managed backends, when they land, return the same
shape). This unit ships ``local``; a ``managed`` backend named in config raises
:class:`UnsupportedBackendError` at construction, surfaced here as a clean
``failed`` :class:`StepResult`.

Why not reuse ``verify_bridge.dbt_run_result_to_check_result``: that maps a
``DbtRunResult`` to the agent's ``CheckResult`` (a different consumer). The
per-model/status extraction *idea* is reused, but the target is a
:class:`StepResult`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from carve.core.config.schema import ComponentType
from carve.core.dbt_execution.backend import DbtCommand
from carve.core.dbt_execution.local import UnsupportedBackendError, build_backend
from carve.core.dbt_execution.result import STATUS_SUCCESS
from carve.integrations.component_locator import ComponentResolutionError
from carve.runtime.component_resolution import resolve_dbt_component
from carve.runtime.step_executor import StepResult

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.pipeline_schema import DbtStepConfig, PipelineStep
    from carve.core.config.schema import ComponentConfig
    from carve.core.dbt_execution.backend import DbtBackend
    from carve.core.dbt_execution.result import DbtRunResult, PerModelResult
    from carve.integrations.component_locator import ResolvedComponent
    from carve.runtime.run_context import PipelineRun

# Default per-step wall-clock budget for a dbt run (spec §"Step executor: dbt"
# / ARCHITECTURE §14.6: 1h). The shipped LocalDbtBackend defaults to 1800s; the
# executor passes this through so the pipeline default is the spec's 3600s.
DEFAULT_DBT_TIMEOUT_SECONDS = 3600

# dbt's raw per-test "failing" status (vs. a build node's ``error``).
_TEST_FAIL_STATUS = "fail"

# The injected backend constructor seam: resolve+config → a DbtBackend.
BackendFactory = Callable[..., "DbtBackend"]


class DbtStepExecutor:
    """Run a ``dbt`` step: resolve the component, run the backend, map the result."""

    step_type = "dbt"

    def __init__(
        self,
        *,
        dbt_executable: str,
        components: dict[str, ComponentConfig] | None = None,
        backend_factory: BackendFactory | None = None,
        timeout_seconds: int = DEFAULT_DBT_TIMEOUT_SECONDS,
    ) -> None:
        """Build the executor.

        Args:
            dbt_executable: The resolved engine binary (argv[0]) — **injected**,
                never installed here (connect's lazy install is deferred). Tests
                pass a fake engine; production passes the resolved Fusion/dbt
                binary path.
            components: ``[components.*]`` blocks for name resolution.
            backend_factory: The injected backend constructor (defaults to the
                shipped ``build_backend`` → ``LocalDbtBackend``). Tests inject a
                fake backend so no real dbt is needed.
            timeout_seconds: Per-step wall-clock budget for the dbt run.
        """
        self._dbt_executable = dbt_executable
        self._components = components or {}
        self._backend_factory = backend_factory or _default_backend_factory
        self._timeout_seconds = timeout_seconds

    async def execute(
        self,
        *,
        step: PipelineStep,
        run: PipelineRun,
        paths: ProjectPaths,
    ) -> StepResult:
        """Resolve, run, and map one ``dbt`` step into a :class:`StepResult`."""
        dbt_step = _as_dbt_step(step)

        try:
            resolved = resolve_dbt_component(dbt_step.component, paths, components=self._components)
        except ComponentResolutionError as exc:
            return StepResult(
                status="failed",
                error_message=f"dbt component did not resolve: {exc}",
            )

        component_block = self._components.get(dbt_step.component) if dbt_step.component else None
        try:
            backend = self._backend_factory(
                resolved=resolved,
                component=component_block,
                dbt_executable=self._dbt_executable,
                timeout_seconds=self._timeout_seconds,
            )
        except UnsupportedBackendError as exc:
            return StepResult(status="failed", error_message=str(exc))

        # FIX-DB1: DbtStepConfig does not cross-validate command vs full_refresh,
        # so a user-authorable `command="test"|"snapshot"` + `full_refresh=true`
        # makes DbtCommand raise pydantic ValidationError (a ValueError). Catch it
        # → clean `failed` StepResult, never an exception out of execute().
        try:
            command = DbtCommand(
                command=dbt_step.command,
                select=(dbt_step.select,) if dbt_step.select else (),
                exclude=(dbt_step.exclude,) if dbt_step.exclude else (),
                vars=dbt_step.vars or None,
                target=run.target,
                full_refresh=dbt_step.full_refresh,
            )
        except ValueError as exc:
            return StepResult(
                status="failed",
                error_message=f"invalid dbt command for step {dbt_step.id}: {exc}",
            )

        # Offload the blocking subprocess to a thread — the DAG walk is async.
        result = await asyncio.to_thread(backend.run, command)
        return _run_result_to_step_result(result, command=dbt_step.command)


def _as_dbt_step(step: PipelineStep) -> DbtStepConfig:
    """Narrow ``step`` to a ``dbt`` step (the registry guarantees the type)."""
    from carve.core.config.pipeline_schema import DbtStepConfig

    if not isinstance(step, DbtStepConfig):
        raise TypeError(f"DbtStepExecutor received a {step.type!r} step: {step.id!r}")
    return step


def _run_result_to_step_result(result: DbtRunResult, *, command: str) -> StepResult:
    """Map a backend-uniform :class:`DbtRunResult` to a :class:`StepResult`.

    Backend-uniform: the mapping reads only the ``DbtRunResult`` surface, so
    an identical result maps to an identical :class:`StepResult` regardless of
    which backend produced it. ``succeeded`` iff ``status == "success"``;
    ``outputs`` carries the run status + per-model detail; a failing ``test``
    command surfaces its failing tests in ``error_message``.
    """
    succeeded = result.status == STATUS_SUCCESS
    outputs: dict[str, object] = {
        "status": result.status,
        "per_model": [_per_model_dict(pm) for pm in result.per_model],
    }
    log_lines = result.logs.splitlines() if result.logs else []

    error_message: str | None = None
    if not succeeded:
        error_message = _failure_message(result, command=command)

    return StepResult(
        status="succeeded" if succeeded else "failed",
        outputs=outputs,
        log_lines=log_lines,
        error_message=error_message,
        duration_ms=result.duration_ms,
    )


def _per_model_dict(pm: PerModelResult) -> dict[str, object]:
    """JSON-serializable per-model record for the ``outputs`` namespace."""
    return {
        "unique_id": pm.unique_id,
        "name": pm.name,
        "status": pm.status,
        "message": pm.message,
        "failures": pm.failures,
    }


def _failure_message(result: DbtRunResult, *, command: str) -> str:
    """Build the ``error_message`` for a non-succeeded dbt run.

    A ``test`` command surfaces its failing tests by name (the spec's
    "``command == 'test'`` → failing tests in ``error_message``"); other
    commands surface the failing/error nodes generically. ``status == "error"``
    (no readable artifact) gets its own message.
    """
    if result.status == "error":
        return "dbt run produced no readable run_results.json (fail-closed)."

    failing = [pm for pm in result.per_model if pm.status not in ("success", "pass", "skipped")]
    if command == "test":
        tests = [pm for pm in failing if pm.status == _TEST_FAIL_STATUS]
        if tests:
            named = ", ".join(f"{pm.name} ({pm.failures})" for pm in tests)
            return f"dbt test: {len(tests)} failing test(s): {named}."
    if failing:
        named = ", ".join(f"{pm.name} ({pm.status})" for pm in failing)
        return f"dbt {command}: {len(failing)} failing node(s): {named}."
    return f"dbt {command} failed (status={result.status})."


def _default_backend_factory(
    *,
    resolved: ResolvedComponent,
    component: ComponentConfig | None,
    dbt_executable: str,
    timeout_seconds: int,
) -> DbtBackend:
    """Construct the shipped ``local`` backend for a resolved dbt component.

    Reads the dbt-execution backend-selecting fields off the component block
    (``dbt_backend``/``dbt_env``/``profiles_dir``) when present; a convention-
    discovered component (no block) defaults to the bundled ``local`` backend.
    The engine binary is the injected ``dbt_executable`` — never installed here.
    """
    assert resolved.type is ComponentType.DBT  # resolver guarantees this
    return build_backend(
        dbt_backend=component.dbt_backend if component else None,
        dbt_executable=dbt_executable,
        project_dir=resolved.code_path,
        dbt_env=component.dbt_env if component else None,
        profiles_dir=component.profiles_dir if component else None,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "DEFAULT_DBT_TIMEOUT_SECONDS",
    "BackendFactory",
    "DbtStepExecutor",
]
