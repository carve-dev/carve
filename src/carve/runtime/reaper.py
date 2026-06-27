"""The reaper loop — reclaim jobs from crashed / unreachable workers.

This completes the queue's crash-recovery story. A worker that crashes
(``kill -9``) or becomes unreachable stops stamping its claimed job's
``heartbeat_at``; once that heartbeat goes stale (``< now - stale_threshold_s``)
the job is stuck ``claimed``/``running`` forever with no live owner. The reaper
periodically scans for those stale jobs and reclaims them:

1. ``job_queue.reclaim_stale(now)`` — ONE atomic ``UPDATE ... RETURNING`` flips
   every stale ``claimed``/``running`` job back to ``queued`` (claim + heartbeat
   cleared) so the next worker re-runs it from scratch. The atomicity means two
   reapers cannot double-reclaim the same job.
2. For each reclaimed job that had an in-flight ``runs`` row, mark that Run
   ``failed`` with ``'worker_crashed_or_unreachable'`` (the orphaned run is dead;
   the re-claim starts a fresh run). A reclaimed-but-never-transitioned job has
   ``run_id IS NULL`` and is skipped here.
3. Emit ``job.reclaimed`` via the no-op ``_emit`` seam (no ``events`` table this
   slice).

Like the scheduler, the module splits a **synchronous, deterministic single
pass** (:func:`reap_stale_once`) — driven sleep-free under a ``FakeClock`` in
tests — from the **async boundary loop** (:func:`reaper_loop`) that ``carve
serve`` hosts. The sync state-store calls are bridged off the event loop via
``asyncio.to_thread`` exactly as ``worker.py``/``scheduler.py`` do.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from carve.runtime.clock import Clock, system_clock

if TYPE_CHECKING:
    from datetime import datetime

    from carve.core.state.job_queue import JobQueue
    from carve.core.state.repository import Repository

logger = logging.getLogger(__name__)

DEFAULT_REAPER_INTERVAL_S = 30.0
DEFAULT_STALE_THRESHOLD_S = 60.0

# The terminal Run state + error stamped on a reclaimed job's orphaned run.
_RECLAIM_REASON = "worker_crashed_or_unreachable"


def reap_stale_once(
    job_queue: JobQueue,
    repository: Repository,
    now: datetime,
    *,
    stale_threshold_s: float = DEFAULT_STALE_THRESHOLD_S,
    tenant_id: int = 1,
) -> list[str]:
    """Reclaim every stale job in one pass; return the reclaimed job ids.

    One synchronous pass (no sleep) so tests drive the reaper deterministically.
    Reclaims via the atomic ``reclaim_stale`` (so a concurrent reaper can't
    double-reclaim), then for each reclaimed job fails its in-flight ``runs`` row
    (skipping a NULL ``run_id`` — a claimed-but-never-transitioned job has no run
    yet) and emits ``job.reclaimed`` through the queue's no-op ``_emit`` seam.
    """
    reclaimed = job_queue.reclaim_stale(
        now, stale_threshold_s=stale_threshold_s, tenant_id=tenant_id
    )
    for job_id, run_id, prior_claimed_by in reclaimed:
        if run_id is not None:
            # The orphaned in-flight run is dead — its worker is gone. The
            # re-claim starts a fresh run; this one is terminal failed.
            repository.update_run_status(run_id, "failed", _RECLAIM_REASON)
        job_queue._emit(
            "job.reclaimed",
            {
                "job_id": job_id,
                "run_id": run_id,
                "prior_claimed_by": prior_claimed_by,
                "reason": "stale_heartbeat",
            },
        )
    if reclaimed:
        logger.info("reaper reclaimed %d stale job(s)", len(reclaimed))
    return [job_id for job_id, _run_id, _claimed_by in reclaimed]


async def reaper_loop(
    job_queue: JobQueue,
    repository: Repository,
    *,
    interval_s: float = DEFAULT_REAPER_INTERVAL_S,
    stale_threshold_s: float = DEFAULT_STALE_THRESHOLD_S,
    clock: Clock = system_clock,
    shutdown: asyncio.Event | None = None,
    tenant_id: int = 1,
) -> None:
    """Poll ``reap_stale_once`` to the next wall-clock boundary until ``shutdown``.

    The async entry point ``carve serve`` runs alongside the scheduler. Each
    iteration bridges the sync ``reap_stale_once`` off the event loop via
    ``asyncio.to_thread``, then sleeps to the next ``interval_s`` boundary via
    ``clock`` (boundary-aligned, so a slow pass doesn't drift). A pass that raises
    is logged and swallowed so one bad poll never kills the loop — it backs off
    via the boundary sleep. ``shutdown`` (an ``asyncio.Event``) breaks the loop
    between sleeps for a clean stop. Mirrors ``scheduler_loop``.
    """
    shutdown = shutdown or asyncio.Event()
    while not shutdown.is_set():
        now = clock.now()
        try:
            await asyncio.to_thread(
                reap_stale_once,
                job_queue,
                repository,
                now,
                stale_threshold_s=stale_threshold_s,
                tenant_id=tenant_id,
            )
        except Exception:
            logger.exception("reaper pass failed; backing off to next boundary")
        if shutdown.is_set():
            break
        # Race the boundary sleep against shutdown so Ctrl-C/SIGTERM doesn't wait
        # out the full interval.
        sleeper = asyncio.create_task(clock.sleep_until_next_boundary(interval_s))
        waiter = asyncio.create_task(shutdown.wait())
        try:
            await asyncio.wait(
                {sleeper, waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (sleeper, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, waiter, return_exceptions=True)


__all__ = [
    "DEFAULT_REAPER_INTERVAL_S",
    "DEFAULT_STALE_THRESHOLD_S",
    "reap_stale_once",
    "reaper_loop",
]
