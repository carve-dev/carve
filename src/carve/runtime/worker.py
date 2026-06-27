"""The minimal runtime worker — claim a job, run it, persist the result.

This is the smallest worker that closes the queue → run → persist loop:

    claim_next  →  create a ``runs`` row  →  transition_to_running
        →  execute_pipeline(..., sink=PersistingStepSink(...))
        →  mark_finished (job) + update_run_status (run) terminal

It is the first caller to inject the **real** persisting :class:`StepSink`, so a
run's ``step_runs`` rows + its terminal ``runs`` row + the terminal ``jobs`` row
all land for the first time.

Scope (this slice)
------------------
``run_once`` claims and runs **one** job (or no-ops on an empty queue);
``worker_loop`` polls ``run_once`` on an interval until cancelled. The worker
registers a ``workers`` row at startup, unregisters at clean shutdown, and
stamps ``heartbeat_at`` once at claim. The heartbeat *loop* + reaper, the
worker-pool fan-out (``--workers N`` as real asyncio), scheduler, and crash
recovery are deferred to later runtime slices.

The sync/async seam
-------------------
``execute_pipeline`` is async; the state store + :class:`JobQueue` are sync.
The worker bridges every sync DB call off the event loop via
:func:`asyncio.to_thread` so DB I/O never blocks the loop (no async engine).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import carve.runtime.heartbeat as heartbeat
from carve.core.state.job_queue import PipelineAlreadyRunning
from carve.runtime.clock import Clock, system_clock
from carve.runtime.execute_pipeline import execute_pipeline
from carve.runtime.persisting_step_sink import PersistingStepSink
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_types.registry import build_step_executor_registry

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.schema import ComponentConfig, ConnectionsConfig
    from carve.core.state.job_queue import JobQueue
    from carve.core.state.repository import Repository
    from carve.runtime.execute_pipeline import RunResult
    from carve.runtime.step_executor import StepExecutorRegistry

logger = logging.getLogger(__name__)

# A registry builder seam: production uses the real
# ``build_step_executor_registry``; tests inject one wired with fake dlt/dbt/sql
# seams so the worker runs end-to-end creds-free over DuckDB.
RegistryFactory = Callable[[], "StepExecutorRegistry"]

# RunResult.status (succeeded/failed/partial) -> the runs table's terminal
# vocabulary (success/failed/...) and the jobs table's terminal vocabulary.
_RUN_STATUS_BY_RESULT = {"succeeded": "success", "failed": "failed", "partial": "failed"}
_JOB_STATUS_BY_RESULT = {"succeeded": "succeeded", "failed": "failed", "partial": "failed"}

DEFAULT_POLL_INTERVAL_S = 1.0


def make_worker_id() -> str:
    """Build a worker id ``<hostname>:<pid>:<startup-uuid>`` (the spec's shape).

    Stable for the life of one worker process, unique across restarts (the
    uuid is fresh per process start).
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class WorkerContext:
    """Everything a worker needs to turn a job into a persisted run.

    Bundles the state-store handles (sync ``Repository`` + ``JobQueue``) and the
    control-plane resolution the registry build needs (``paths``,
    ``connections``, ``dbt_executable``, optional ``components``). Built once by
    the ``carve worker`` command / a test and reused across claims.
    """

    repository: Repository
    job_queue: JobQueue
    paths: ProjectPaths
    connections: ConnectionsConfig
    dbt_executable: str
    components: dict[str, ComponentConfig] | None = None
    worker_id: str = ""
    # Injectable for tests: a registry wired with fake dlt/dbt/sql seams so the
    # worker runs end-to-end creds-free. ``None`` -> the real builder.
    registry_factory: RegistryFactory | None = None
    # The time/sleep seam for the heartbeat loop — ``system_clock`` in
    # production; a ``FakeClock`` in tests drives the heartbeat sleep-free.
    clock: Clock = system_clock

    def build_registry(self) -> StepExecutorRegistry:
        """Build the dlt→dbt→sql registry for a run.

        Reuses :func:`build_step_executor_registry` exactly as
        ``pipeline_verify`` does — the same wiring, no new execution path. A
        test may inject ``registry_factory`` to supply fake executor seams.
        """
        if self.registry_factory is not None:
            return self.registry_factory()
        return build_step_executor_registry(
            connections=self.connections,
            dbt_executable=self.dbt_executable,
            components=self.components or {},
        )


async def run_once(ctx: WorkerContext) -> bool:
    """Claim and run at most one job. Return ``True`` if a job ran, else ``False``.

    On an empty queue (nothing claimable) this is a clean no-op returning
    ``False`` — the idempotency the loop and ``--once`` rely on. On a claim:

    1. Create a ``runs`` row (``kind='pipeline'``).
    2. ``transition_to_running`` — on :class:`PipelineAlreadyRunning`, release
       the claim and return ``False`` (another worker owns this pipeline's run).
    3. Run ``execute_pipeline`` with the persisting sink + a freshly built
       registry, then mark the job + run terminal from the derived status.

    **Once a job is claimed it is ours, so ANY failure after the claim — a
    setup DB error (create_run / transition / status write) just as much as an
    execute error — resolves the claim by marking the job + run ``failed``
    (best-effort), never leaving it orphaned ``claimed``/``running``.** That
    matters because the reaper that would otherwise reclaim a stuck job is
    deferred to a later slice, so an orphan would block the pipeline forever.
    """
    worker_id = ctx.worker_id or make_worker_id()
    job = await asyncio.to_thread(ctx.job_queue.claim_next, worker_id)
    if job is None:
        return False

    run_id: str | None = None
    try:
        run_id = await asyncio.to_thread(
            ctx.repository.create_run,
            "pipeline",
            job.id,
            pipeline_name=None,
            target=job.target,
        )
        try:
            transitioned = await asyncio.to_thread(
                ctx.job_queue.transition_to_running,
                job.id,
                run_id,
                expected_worker_id=worker_id,
            )
        except PipelineAlreadyRunning:
            # Another job for this pipeline is already running; back off cleanly
            # (release the claim, cancel this run) — not a failure.
            await asyncio.to_thread(ctx.job_queue.release_claim, job.id)
            await asyncio.to_thread(
                ctx.repository.update_run_status,
                run_id,
                "cancelled",
                "pipeline already running",
            )
            return False

        if not transitioned:
            # The ownership guard no-opped: this worker stalled past the reaper
            # threshold, was reclaimed, and the job is now queued / owned by a
            # new worker. We lost the claim — do NOT run (the new owner will).
            # Cancel our orphaned run; the reclaim left the job for the new
            # owner, so there is nothing to fail-stomp here.
            logger.warning(
                "worker %s lost claim on job %s (reclaimed); skipping", worker_id, job.id
            )
            await asyncio.to_thread(
                ctx.repository.update_run_status,
                run_id,
                "cancelled",
                "claim lost (reclaimed by reaper)",
            )
            return False

        await asyncio.to_thread(ctx.repository.update_run_status, run_id, "running")
        handle = heartbeat.start(ctx.job_queue, job.id, clock=ctx.clock, worker_id=worker_id)
        try:
            result = await _execute_job(
                ctx, job_pipeline=job.pipeline, run_id=run_id, target=job.target
            )
        finally:
            # Stop the heartbeat the instant execution ends (success OR error) so
            # a beat can never leak past job completion.
            await handle.stop()
    except Exception as exc:
        # Setup or execute failure on a claimed job: resolve the claim so it is
        # never orphaned (the reaper is deferred this slice). Best-effort — a
        # down DB may also reject these writes, but the common single-op hiccup
        # is recovered, and `worker_loop` survives regardless.
        logger.exception("worker job %s failed; marking failed", job.id)
        await _fail_job(ctx, job_id=job.id, run_id=run_id, error=str(exc), worker_id=worker_id)
        return True

    await _finalize(ctx, job_id=job.id, run_id=run_id, result=result, worker_id=worker_id)
    return True


async def _fail_job(
    ctx: WorkerContext, *, job_id: str, run_id: str | None, error: str, worker_id: str
) -> None:
    """Best-effort mark the run (if created) + the job terminal ``failed``.

    Each write is suppressed independently so a failure on one (e.g. the run
    row was never created) still attempts the other — the job claim must be
    resolved even when the run side can't be. The ``mark_finished`` is
    ownership-guarded (``expected_worker_id``): a reclaimed job's fail-write
    correctly no-ops rather than stomping the new owner.
    """
    if run_id is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ctx.repository.update_run_status, run_id, "failed", error)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(
            ctx.job_queue.mark_finished,
            job_id,
            "failed",
            error_message=error,
            expected_worker_id=worker_id,
        )


async def _execute_job(
    ctx: WorkerContext,
    *,
    job_pipeline: str,
    run_id: str,
    target: str,
) -> RunResult:
    """Dispatch ``execute_pipeline`` for the claimed job with the real sink."""
    registry = ctx.build_registry()
    sink = PersistingStepSink(run_id=run_id, job_queue=ctx.job_queue)
    pipeline_run = PipelineRun(pipeline=job_pipeline, target=target, trigger="manual", id=run_id)
    return await execute_pipeline(
        pipeline_run,
        paths=ctx.paths,
        registry=registry,
        components=ctx.components or {},
        sink=sink,
    )


async def _finalize(
    ctx: WorkerContext,
    *,
    job_id: str,
    run_id: str,
    result: RunResult,
    worker_id: str,
) -> None:
    """Stamp the job + run terminal from the derived ``RunResult.status``.

    The job's terminal write is ownership-guarded (``expected_worker_id``): if
    this worker was reclaimed mid-execute, the finalize no-ops rather than
    stomping the job a new owner now holds.
    """
    run_status = _RUN_STATUS_BY_RESULT.get(result.status, "failed")
    job_status = _JOB_STATUS_BY_RESULT.get(result.status, "failed")
    error = None if result.status == "succeeded" else f"run {result.status}"
    await asyncio.to_thread(ctx.repository.update_run_status, run_id, run_status, error)
    await asyncio.to_thread(
        ctx.job_queue.mark_finished,
        job_id,
        job_status,
        error_message=error,
        expected_worker_id=worker_id,
    )


async def worker_loop(
    ctx: WorkerContext,
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    shutdown: asyncio.Event | None = None,
) -> None:
    """Register, poll ``run_once`` until ``shutdown`` is set, then unregister.

    Registers a ``workers`` row on entry and marks it stopped on exit (even on
    cancellation). Between claims it sleeps ``poll_interval_s``; a claimed job
    runs immediately and the loop re-polls without sleeping. The single-worker
    drain for this slice — the worker-pool fan-out is deferred.
    """
    worker_id = ctx.worker_id or make_worker_id()
    ctx = _with_worker_id(ctx, worker_id)
    shutdown = shutdown or asyncio.Event()

    await asyncio.to_thread(
        ctx.job_queue.register_worker,
        worker_id,
        host=socket.gethostname(),
        pid=os.getpid(),
    )
    try:
        while not shutdown.is_set():
            try:
                ran = await run_once(ctx)
            except Exception:
                # `run_once` resolves a claimed job's own failures; this guards
                # the residual (e.g. `claim_next` itself erroring) so one bad
                # poll never kills the worker — back off via the poll sleep.
                logger.exception("worker poll failed; backing off")
                ran = False
            if ran:
                continue
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval_s)
            except TimeoutError:
                pass
    finally:
        await asyncio.to_thread(ctx.job_queue.unregister_worker, worker_id)


def _with_worker_id(ctx: WorkerContext, worker_id: str) -> WorkerContext:
    """Return ``ctx`` with ``worker_id`` set (so claim/register agree on the id)."""
    if ctx.worker_id == worker_id:
        return ctx
    return WorkerContext(
        repository=ctx.repository,
        job_queue=ctx.job_queue,
        paths=ctx.paths,
        connections=ctx.connections,
        dbt_executable=ctx.dbt_executable,
        components=ctx.components,
        worker_id=worker_id,
        registry_factory=ctx.registry_factory,
        clock=ctx.clock,
    )


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "WorkerContext",
    "make_worker_id",
    "run_once",
    "worker_loop",
]
