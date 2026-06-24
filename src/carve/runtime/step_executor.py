"""The step-executor seam: ``StepResult`` + ``StepExecutor`` + registry.

.. warning::

   **Forward-declared seam — runtime (Increment 4) assumes ownership.**
   This unit (the deterministic composition core) ships the *minimal*
   contract :func:`carve.runtime.execute_pipeline.execute_pipeline` needs:
   a pipelines-local :class:`StepResult`, a :class:`StepExecutor`
   ``Protocol``, and a :class:`StepExecutorRegistry`. The Increment-4
   runtime will own/reconcile this seam (relocating or extending it as it
   wires real ``step_runs`` persistence and ``step.*`` events). Treat the
   shapes here as the agreed interface the concrete dlt/dbt/sql executors
   (Unit 2) implement, not a finished public API.

Why a pipelines-local ``StepResult`` (and not M1's ``core/steps/base.py``)
--------------------------------------------------------------------------
The M1 ``core/steps/base.StepResult`` is a different, narrower thing: it
is a config-holder for the M1 ``Runner`` whose ``status`` vocabulary is
``"success"/"failed"/"cancelled"``, and whose companion ``Step`` is a
*structural config* protocol (``config``/``step_type``/``validate``), not
the ``async execute(*, step, run, paths) -> StepResult`` an executor runs.
The pipeline spec's status vocabulary is ``"succeeded"`` (not
``"success"``), and reusing the M1 model would force a vocabulary
collision and entangle the M1 Python-step/``Runner`` model with the
runtime DAG model. So this seam defines its own ``StepResult``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.pipeline_schema import PipelineStep
    from carve.runtime.run_context import PipelineRun


StepStatus = Literal["succeeded", "failed", "skipped"]


class StepResult(BaseModel):
    """The outcome of one step execution.

    ``status`` is the spec's vocabulary (``"succeeded"``/``"failed"``/
    ``"skipped"``) — deliberately distinct from M1's ``"success"`` (see the
    module docstring). ``outputs`` is the structured dict threaded into the
    cross-step Jinja context (conceptually 64KB-capped — the *enforcement*
    of that cap is runtime's persistence concern, not this in-memory
    value's). ``log_lines``/``error_message``/``duration_ms`` and the
    optional timestamps carry what the runtime persists into ``step_runs``.
    """

    model_config = ConfigDict(extra="forbid")

    status: StepStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    log_lines: list[str] = Field(default_factory=list)
    error_message: str | None = None
    duration_ms: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def skipped(cls, *, error_message: str | None = None) -> StepResult:
        """A canned ``skipped`` result (a step a failure mode bypassed)."""
        return cls(status="skipped", error_message=error_message)


@runtime_checkable
class StepExecutor(Protocol):
    """The contract every concrete step type (dlt/dbt/sql) implements.

    A step executor takes a fully-resolved :class:`PipelineStep` plus the
    run context and project paths, runs the work (a dlt subprocess, a dbt
    backend call, a sql statement), and returns a :class:`StepResult`. It
    is ``async`` because the DAG walk launches independent ready steps
    concurrently; an executor that does blocking work offloads it (e.g.
    ``asyncio.to_thread`` / a subprocess) rather than blocking the loop.

    The concrete executors are **Unit 2's** concern; ``execute_pipeline``
    is provable now against *stub* executors implementing this Protocol.
    """

    @property
    def step_type(self) -> str:
        """The ``type`` discriminator this executor handles (dlt/dbt/sql)."""
        ...

    async def execute(
        self,
        *,
        step: PipelineStep,
        run: PipelineRun,
        paths: ProjectPaths,
    ) -> StepResult:
        """Run ``step`` against ``run``/``paths`` and return its result."""
        ...


class StepExecutorRegistry:
    """Maps a step ``type`` to the :class:`StepExecutor` that runs it.

    ``execute_pipeline`` looks an executor up by ``step.type`` and
    dispatches to it. In production the runtime registers the three real
    executors (dlt/dbt/sql); in tests a stub executor is registered per
    type to prove the DAG mechanics without any real backend.
    """

    def __init__(self) -> None:
        self._by_type: dict[str, StepExecutor] = {}

    def register(self, executor: StepExecutor) -> None:
        """Register ``executor`` under its ``step_type`` (replaces any prior)."""
        self._by_type[executor.step_type] = executor

    def lookup(self, step_type: str) -> StepExecutor:
        """Return the executor for ``step_type``.

        Raises:
            KeyError: If no executor is registered for ``step_type``.
        """
        try:
            return self._by_type[step_type]
        except KeyError:
            raise KeyError(
                f"No step executor registered for step type {step_type!r}. "
                f"Registered: {sorted(self._by_type)}."
            ) from None

    def __contains__(self, step_type: object) -> bool:
        return step_type in self._by_type


__all__ = [
    "StepExecutor",
    "StepExecutorRegistry",
    "StepResult",
    "StepStatus",
]
