"""The real, persisting :class:`StepSink` — the seam fulfilled.

``execute_pipeline`` has carried a forward-declared :class:`StepSink` Protocol
since Increment 3, defaulting to a no-op so the DAG walk stayed
runtime-independent (no ``step_runs`` rows, no events). This is the runtime's
real sink: ``step_started`` inserts a ``running`` ``step_runs`` row,
``step_finished`` transitions it to the step's terminal status with the
threaded ``outputs``/``error``/timings. This is the first time
``execute_pipeline`` persists anything.

The sync/async seam (a load-bearing invariant)
-----------------------------------------------
The sink's hooks are ``async`` (so a persisting sink can do I/O without
blocking the DAG walk), but the state store is **synchronous** SQLAlchemy. So
every DB call is bridged off the event loop via :func:`asyncio.to_thread` — the
queue stays sync, the loop never blocks. No async DB engine this slice.

The ``step.*`` event emit stays a no-op seam
--------------------------------------------
The spec pairs ``step_runs`` persistence with ``step.started``/
``step.completed``/``step.failed`` events, but the ``events`` table + emitter
are a later runtime slice. The emit points are marked below so the signature is
event-ready; persistence is this slice's deliverable.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from carve.core.config.pipeline_schema import PipelineStep
    from carve.core.state.job_queue import JobQueue
    from carve.runtime.run_context import PipelineRun
    from carve.runtime.step_executor import StepResult


class PersistingStepSink:
    """A :class:`~carve.runtime.execute_pipeline.StepSink` that writes ``step_runs``.

    Tracks the ``step_runs`` row id between ``step_started`` and
    ``step_finished`` for each ``(step_id, attempt)`` so the finish call updates
    the row the start call inserted (rather than inserting a second row).
    Constructed per run with the run's id and the shared (sync) :class:`JobQueue`.
    """

    def __init__(self, *, run_id: str, job_queue: JobQueue) -> None:
        self._run_id = run_id
        self._job_queue = job_queue
        # (step_id, attempt) -> step_runs.id, threaded start -> finish.
        self._open: dict[tuple[str, int], str] = {}

    async def step_started(
        self,
        *,
        step: PipelineStep,
        run: PipelineRun,
        attempt: int,
    ) -> None:
        """Insert a ``running`` ``step_runs`` row for this attempt."""
        del run  # the run id is fixed at construction; param kept for the Protocol
        step_run_id = await asyncio.to_thread(
            self._job_queue.create_step_run,
            run_id=self._run_id,
            step_id=step.id,
            step_type=step.type,
            attempt=attempt,
        )
        self._open[(step.id, attempt)] = step_run_id
        # TODO(events slice): emit step.started here once the events table lands.

    async def step_finished(
        self,
        *,
        step: PipelineStep,
        run: PipelineRun,
        result: StepResult,
        attempt: int,
    ) -> None:
        """Transition this attempt's ``step_runs`` row to its terminal status."""
        del run  # see step_started
        step_run_id = self._open.pop((step.id, attempt), None)
        if step_run_id is None:
            # step_finished without a matching step_started: insert a fresh
            # terminal row so the step is still recorded (defensive — the DAG
            # walk always pairs them, but a future caller might not).
            step_run_id = await asyncio.to_thread(
                self._job_queue.create_step_run,
                run_id=self._run_id,
                step_id=step.id,
                step_type=step.type,
                attempt=attempt,
            )
        await asyncio.to_thread(
            self._job_queue.finish_step_run,
            step_run_id,
            status=result.status,
            outputs=result.outputs,
            error_message=result.error_message,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
        )
        # TODO(events slice): emit step.completed / step.failed here.


__all__ = ["PersistingStepSink"]
