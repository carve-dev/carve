"""The scheduler loop — fire due schedules onto the job queue, once per window.

Each pass: ``schedules.list_due(now)`` → for every due schedule,
``job_queue.enqueue_scheduled(pipeline, target, scheduled_for=this_tick)`` then
``schedules.set_last_fired(id, now)`` (which advances ``next_fires_at`` so the
row leaves the due window). The two correctness backstops:

* **Dedup → skip, never double-enqueue.** Two passes inside one cron window race
  the shipped ``ix_jobs_one_queued_per_pipeline`` partial index — the second
  ``enqueue_scheduled`` raises :class:`QueuedJobAlreadyExists`, which the loop
  turns into a ``schedule.skipped`` emit and continues. Combined with the
  ``next_fires_at`` advance, a healthy schedule fires exactly once per window.
* **Determinism.** All time comes from an injected :class:`Clock`; the loop
  sleeps to the next wall-clock interval boundary via ``clock`` (keeping
  ``*/5``-style schedules aligned), never ``now + interval`` and never a real
  ``time.sleep``. Tests drive :func:`run_due_once` under a ``FakeClock``.

The sync repo/queue calls are bridged off the event loop via ``asyncio.to_thread``
exactly as the shipped ``worker.py`` does. ``carve serve`` runs
:func:`scheduler_loop` as a single asyncio task with graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from carve.core.state.job_queue import QueuedJobAlreadyExists
from carve.runtime.clock import Clock, system_clock
from carve.runtime.cron import this_tick_at

if TYPE_CHECKING:
    from datetime import datetime

    from carve.core.state.job_queue import JobQueue
    from carve.core.state.schedules import Schedules

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 30.0


def run_due_once(
    schedules: Schedules,
    job_queue: JobQueue,
    now: datetime,
    *,
    tenant_id: int = 1,
) -> int:
    """Fire every schedule due at ``now``; return the number actually enqueued.

    One synchronous pass (no sleep) so tests can drive the scheduler
    deterministically. For each due row it stamps the enqueued job's
    ``scheduled_for`` with the **canonical cron tick** (``this_tick_at`` — so two
    passes in one window enqueue the same ``scheduled_for`` and the dedup index
    sees a single window), then advances ``next_fires_at`` via
    ``set_last_fired``.

    On :class:`QueuedJobAlreadyExists` (a queued job for this pipeline already
    exists — a second pass in the same window, or a still-queued prior fire) it
    emits ``schedule.skipped`` and continues; the row's ``next_fires_at`` is
    **still advanced** so the schedule doesn't keep re-hitting the dedup path
    every tick. A double-enqueue is impossible by construction.
    """
    due = schedules.list_due(now, tenant_id=tenant_id)
    fired = 0
    for schedule in due:
        scheduled_for = this_tick_at(schedule.cron, now, schedule.timezone)
        try:
            job = job_queue.enqueue_scheduled(
                schedule.pipeline,
                schedule.target,
                scheduled_for=scheduled_for,
                tenant_id=tenant_id,
            )
        except QueuedJobAlreadyExists:
            schedules._emit(
                "schedule.skipped",
                {
                    "pipeline": schedule.pipeline,
                    "scheduled_for": scheduled_for.isoformat(),
                    "reason": "queued_job_already_exists",
                },
            )
        else:
            schedules._emit(
                "schedule.fired",
                {
                    "pipeline": schedule.pipeline,
                    "job_id": job.id,
                    "scheduled_for": scheduled_for.isoformat(),
                },
            )
            fired += 1
        # Advance regardless of enqueue vs skip: a skipped row that kept its
        # just-fired next_fires_at would stay due and re-hit dedup every tick.
        schedules.set_last_fired(schedule.id, now)
    return fired


async def scheduler_loop(
    schedules: Schedules,
    job_queue: JobQueue,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    clock: Clock = system_clock,
    shutdown: asyncio.Event | None = None,
    tenant_id: int = 1,
) -> None:
    """Poll ``run_due_once`` to the next wall-clock boundary until ``shutdown``.

    The async entry point ``carve serve`` runs. Each iteration bridges the sync
    ``run_due_once`` off the event loop via ``asyncio.to_thread``, then sleeps to
    the next ``interval_s`` boundary via ``clock`` (boundary-aligned, so a slow
    pass doesn't drift the schedule). A pass that raises is logged and swallowed
    so one bad poll never kills the loop — it backs off via the boundary sleep.
    ``shutdown`` (an ``asyncio.Event``) breaks the loop between sleeps for a clean
    stop.
    """
    shutdown = shutdown or asyncio.Event()
    while not shutdown.is_set():
        now = clock.now()
        try:
            await asyncio.to_thread(run_due_once, schedules, job_queue, now, tenant_id=tenant_id)
        except Exception:
            logger.exception("scheduler pass failed; backing off to next boundary")
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
    "DEFAULT_INTERVAL_S",
    "run_due_once",
    "scheduler_loop",
]
