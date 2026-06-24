"""Carve's pipeline runtime package — **co-owned, built in two units**.

This unit (the *pipelines* capability's deterministic composition core)
ships the half that does **not** need a live scheduler/worker:

* :class:`PipelineDAG` — cycle check, topological order, readiness.
* :func:`execute_pipeline` (+ :class:`RunResult`) — the async, slot-capped
  DAG walk over a :class:`StepExecutorRegistry`, runtime-independent (no
  ``step_runs`` rows, no ``step.*`` events — those go behind an injected
  :class:`StepSink` that defaults to a no-op).
* :class:`StepExecutor` / :class:`StepExecutorRegistry` / :class:`StepResult`
  — the forward-declared executor seam (the concrete dlt/dbt/sql executors
  are Unit 2; this unit proves the mechanics against stubs).
* :func:`apply_failure_mode` / :func:`derive_run_status` — the five-mode
  table + run-status derivation.
* :func:`make_jinja_context` / :func:`render_step_vars` — the sandboxed
  cross-step Jinja namespace.
* :func:`resolve_dlt_component` / :func:`resolve_dbt_component` — name ->
  code-path wrappers over the shipped component locator.

The **Increment-4 ``runtime`` capability extends the same package**
additively: the scheduler/job-queue/worker pool, the definition+seed
reconciler, the ``schedules``/``step_runs`` tables, and the real
:class:`StepSink` that persists rows + emits events. Nothing here is
scheduler/reconciler-shaped; that line is the unit boundary.
"""

from __future__ import annotations

from carve.runtime.component_resolution import resolve_dbt_component, resolve_dlt_component
from carve.runtime.execute_pipeline import (
    DEFAULT_AVAILABLE_SLOTS,
    NoOpStepSink,
    RunResult,
    StepSink,
    execute_pipeline,
)
from carve.runtime.failure_modes import (
    RunState,
    RunStatus,
    apply_failure_mode,
    derive_run_status,
)
from carve.runtime.jinja_context import (
    JinjaRenderError,
    make_jinja_context,
    render_step_vars,
)
from carve.runtime.pipeline_dag import PipelineDAG
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_executor import (
    StepExecutor,
    StepExecutorRegistry,
    StepResult,
    StepStatus,
)

__all__ = [
    "DEFAULT_AVAILABLE_SLOTS",
    "JinjaRenderError",
    "NoOpStepSink",
    "PipelineDAG",
    "PipelineRun",
    "RunResult",
    "RunState",
    "RunStatus",
    "StepExecutor",
    "StepExecutorRegistry",
    "StepResult",
    "StepSink",
    "StepStatus",
    "apply_failure_mode",
    "derive_run_status",
    "execute_pipeline",
    "make_jinja_context",
    "render_step_vars",
    "resolve_dbt_component",
    "resolve_dlt_component",
]
