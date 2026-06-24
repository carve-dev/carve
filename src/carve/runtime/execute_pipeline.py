"""The async DAG walk: ``execute_pipeline``.

This is the deterministic composition core's entry point — the function
the Increment-4 worker calls. It loads + validates a pipeline, walks its
DAG launching independent ready steps concurrently (up to a slot cap),
renders each step's Jinja vars against the running outputs at launch,
dispatches to the registered :class:`StepExecutor`, applies the step's
failure mode, threads successful outputs forward, and returns a
:class:`RunResult` with the derived pipeline status.

Runtime-independence (a hard invariant)
---------------------------------------
``execute_pipeline`` writes **no** ``step_runs`` rows and emits **no**
``step.*`` events — those tables/events are the Increment-4 runtime's. Any
such side effect goes behind an injected :class:`StepSink` that defaults to
a **no-op**, so the runtime supplies the real persisting/event-emitting
sink later **without changing this signature**. Likewise the parallelism
cap (``available_slots``, default 4) and the retry backoff sleeper are
injectable implementation defaults, not wired to a worker here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from carve.core.config.pipeline_schema import load_pipeline
from carve.runtime.failure_modes import RunState, RunStatus, apply_failure_mode, derive_run_status
from carve.runtime.jinja_context import make_jinja_context, render_step_vars
from carve.runtime.pipeline_dag import PipelineDAG
from carve.runtime.step_executor import StepResult

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.pipeline_schema import Pipeline, PipelineStep
    from carve.core.config.schema import ComponentConfig
    from carve.runtime.run_context import PipelineRun
    from carve.runtime.step_executor import StepExecutorRegistry


# Default intra-pipeline parallelism (spec Open question — injectable).
DEFAULT_AVAILABLE_SLOTS = 4

# A sleeper coroutine for retry backoff; injectable so tests run instantly.
Sleeper = Callable[[float], Awaitable[None]]


class StepSink(Protocol):
    """The injected side-effect seam for step lifecycle.

    The runtime (Increment 4) supplies a real sink that writes
    ``step_runs`` rows and emits ``step.started``/``step.completed``/
    ``step.failed`` events. This unit defaults to :class:`NoOpStepSink` so
    the DAG walk stays runtime-independent. Both hooks are ``async`` so a
    persisting sink can do I/O without blocking the loop.
    """

    async def step_started(self, *, step: PipelineStep, run: PipelineRun, attempt: int) -> None:
        """Called just before a step's executor runs (per attempt)."""
        ...

    async def step_finished(
        self,
        *,
        step: PipelineStep,
        run: PipelineRun,
        result: StepResult,
        attempt: int,
    ) -> None:
        """Called after a step's executor returns (per attempt)."""
        ...


class NoOpStepSink:
    """The default :class:`StepSink`: records nothing, emits nothing."""

    async def step_started(self, *, step: PipelineStep, run: PipelineRun, attempt: int) -> None:
        return None

    async def step_finished(
        self,
        *,
        step: PipelineStep,
        run: PipelineRun,
        result: StepResult,
        attempt: int,
    ) -> None:
        return None


@dataclass(frozen=True)
class RunResult:
    """The terminal outcome of a pipeline run.

    Carries the final completed/failed/skipped partition, every successful
    step's outputs, the recorded warnings, and the derived pipeline
    ``status`` (``succeeded``/``failed``/``partial``).
    """

    status: RunStatus
    completed: frozenset[str]
    failed: frozenset[str]
    skipped: frozenset[str]
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_run_state(cls, state: RunState, outputs: dict[str, dict[str, Any]]) -> RunResult:
        return cls(
            status=derive_run_status(state),
            completed=frozenset(state.completed),
            failed=frozenset(state.failed),
            skipped=frozenset(state.skipped),
            outputs=dict(outputs),
            warnings=list(state.warnings),
        )


async def execute_pipeline(
    run: PipelineRun,
    *,
    paths: ProjectPaths,
    registry: StepExecutorRegistry,
    components: dict[str, ComponentConfig] | None = None,
    pipeline: Pipeline | None = None,
    available_slots: int = DEFAULT_AVAILABLE_SLOTS,
    sink: StepSink | None = None,
    sleeper: Sleeper | None = None,
) -> RunResult:
    """Execute ``run``'s pipeline as a slot-capped async DAG walk.

    Args:
        run: The run identity/dispatch context. ``run.pipeline`` names the
            TOML loaded from ``paths.pipelines_dir`` (unless ``pipeline`` is
            passed directly — used by tests over hand-built pipelines).
        paths: Resolved control-plane paths.
        registry: Maps each step ``type`` to its :class:`StepExecutor`.
        components: ``[components.*]`` blocks for name resolution (defaults
            to empty == simple mode).
        pipeline: An already-loaded :class:`Pipeline`, bypassing the TOML
            load (test seam; production passes ``None`` and loads by name).
        available_slots: Max steps launched concurrently per wave.
        sink: The step-lifecycle side-effect sink (defaults to no-op).
        sleeper: The retry-backoff sleeper (defaults to ``asyncio.sleep``).

    Returns:
        A :class:`RunResult` with the derived pipeline status.

    Wave-barrier parallelism (a deliberate Unit-1 simplification)
    -------------------------------------------------------------
    Each loop iteration launches a *wave* (up to ``available_slots`` ready
    steps), ``await``\\s the **whole** wave via ``asyncio.gather``, then
    recomputes readiness. This means a fast step's freed slot is **not**
    reused until the slowest step in the same wave finishes (head-of-line
    blocking) — correct, and the basic-parallelism bar is met, but slot
    reuse is suboptimal. This mirrors the spec pseudocode and is intentional
    here. The Increment-4 runtime may replace this loop with a streaming
    slot model (``asyncio.wait(..., return_when=FIRST_COMPLETED)``) for
    tighter slot reuse, without changing this signature.

    Jinja ``steps`` namespace contract
    -----------------------------------
    Every *resolved* step is recorded into the structure that backs the
    cross-step Jinja ``steps.<id>`` namespace, carrying its **real** status:
    a ``succeeded`` step threads its outputs forward; a tolerated failure
    (``warn``/``continue``/the ``skip_downstream`` step itself) appears with
    ``status="failed"`` and its (typically empty) outputs; a step skipped by
    ``skip_downstream`` appears with ``status="skipped"`` and empty outputs.
    So a downstream template referencing ``steps.<id>.status`` of a
    non-succeeded upstream resolves rather than hitting ``StrictUndefined``.
    """
    if pipeline is None:
        pipeline = load_pipeline(
            paths.pipelines_dir / f"{run.pipeline}.toml",
            components=components or {},
            paths=paths,
        )
    dag = PipelineDAG(pipeline)
    sink = sink or NoOpStepSink()
    sleeper = sleeper or asyncio.sleep

    state = RunState()
    outputs: dict[str, dict[str, Any]] = {}
    # The per-step results that back the Jinja `steps.<id>` namespace.
    step_results: dict[str, StepResult] = {}

    while True:
        if state.halted:
            break
        ready = dag.ready_steps(state.completed, state.failed, state.skipped)
        if not ready:
            break

        wave = ready[:available_slots]
        coros = [
            _run_step_with_retry(
                step=step,
                run=run,
                paths=paths,
                registry=registry,
                step_results=step_results,
                sink=sink,
                sleeper=sleeper,
            )
            for step in wave
        ]
        results = await asyncio.gather(*coros)

        for step, result in zip(wave, results, strict=True):
            skipped_before = set(state.skipped)
            apply_failure_mode(step, result, dag, state)
            # Record every step that RAN (succeeded or a tolerated/exhausted
            # failure) into the Jinja `steps` namespace, carrying its real
            # status; only a succeeded step threads its outputs forward.
            step_results[step.id] = result
            if result.status == "succeeded":
                outputs[step.id] = result.outputs
            # Steps newly skipped by skip_downstream never ran — surface them
            # in the namespace with status="skipped" so templates resolve them.
            for skipped_id in state.skipped - skipped_before:
                step_results.setdefault(skipped_id, StepResult.skipped())

    return RunResult.from_run_state(state, outputs)


async def _run_step_with_retry(
    *,
    step: PipelineStep,
    run: PipelineRun,
    paths: ProjectPaths,
    registry: StepExecutorRegistry,
    step_results: dict[str, StepResult],
    sink: StepSink,
    sleeper: Sleeper,
) -> StepResult:
    """Render Jinja, dispatch to the executor, and orchestrate retries.

    For a ``retry``-mode step, runs up to ``max_attempts`` attempts with
    backoff between failures; returns the first success or the last failure
    (which ``apply_failure_mode`` then classifies as a ``fail``). For every
    other mode, runs exactly once.
    """
    fm = step.failure_mode
    attempts = fm.max_attempts if fm.mode == "retry" else 1
    attempts = max(attempts, 1)

    last_result: StepResult | None = None
    for attempt in range(1, attempts + 1):
        result = await _launch_step(
            step=step,
            run=run,
            paths=paths,
            registry=registry,
            step_results=step_results,
            sink=sink,
            attempt=attempt,
        )
        last_result = result
        if result.status == "succeeded":
            return result
        if attempt < attempts:
            await sleeper(_backoff_delay(fm.backoff, attempt, fm.initial_delay_s, fm.max_delay_s))

    assert last_result is not None  # the loop runs at least once
    return last_result


async def _launch_step(
    *,
    step: PipelineStep,
    run: PipelineRun,
    paths: ProjectPaths,
    registry: StepExecutorRegistry,
    step_results: dict[str, StepResult],
    sink: StepSink,
    attempt: int,
) -> StepResult:
    """Render the step's Jinja vars, then run its executor once.

    Jinja is rendered at launch time (deps already resolved) against the
    running ``step_results``; the rendered map is attached to the step's
    ``jinja_vars`` so the executor sees concrete values. A render failure
    (a sandbox violation, an undefined upstream output) is surfaced as a
    ``failed`` :class:`StepResult` rather than crashing the whole walk.
    """
    context = make_jinja_context(run=run, step_results=step_results)
    try:
        rendered = render_step_vars(
            step_id=step.id,
            jinja_vars=step.jinja_vars,
            context=context,
        )
    except Exception as exc:
        return StepResult(status="failed", error_message=str(exc))

    # Pass the rendered values to the executor as the resolved step config.
    # Mutating a copy keeps the source Pipeline immutable across waves/retries.
    step = step.model_copy(update={"jinja_vars": rendered})

    await sink.step_started(step=step, run=run, attempt=attempt)
    try:
        # Resolve the executor and run it inside one guard: an unexpected raise
        # (no executor registered for the type, a subprocess OSError, a dbt
        # backend/network error, an output-parse error in a real Unit-2
        # executor) degrades to a `failed` StepResult so it flows through the
        # failure-mode state machine instead of escaping `asyncio.gather` and
        # discarding concurrent siblings — symmetric with the Jinja catch above.
        executor = registry.lookup(step.type)
        result = await executor.execute(step=step, run=run, paths=paths)
    except Exception as exc:
        result = StepResult(status="failed", error_message=f"step launch failed: {exc}")
    await sink.step_finished(step=step, run=run, result=result, attempt=attempt)
    return result


def _backoff_delay(
    backoff: str,
    attempt: int,
    initial_delay_s: float,
    max_delay_s: float,
) -> float:
    """Compute the delay before retry ``attempt+1`` for a backoff strategy.

    ``attempt`` is 1-based (the attempt that just failed). ``exponential``
    doubles each step from ``initial``; ``linear`` adds ``initial`` each
    step; ``fixed`` is constant. Every strategy is clamped to ``max``.
    """
    if backoff == "fixed":
        delay = initial_delay_s
    elif backoff == "linear":
        delay = initial_delay_s * attempt
    else:  # exponential (the default)
        delay = initial_delay_s * (2 ** (attempt - 1))
    return min(delay, max_delay_s)


__all__ = [
    "DEFAULT_AVAILABLE_SLOTS",
    "NoOpStepSink",
    "RunResult",
    "StepSink",
    "execute_pipeline",
]
