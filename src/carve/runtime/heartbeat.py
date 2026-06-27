"""The worker heartbeat loop — keep a claimed job's ``heartbeat_at`` fresh.

The reaper reclaims a job whose ``heartbeat_at`` has gone stale (``< now -
stale_threshold_s``). A live worker must therefore prove it is still alive by
stamping ``heartbeat_at`` on an interval while it executes a claimed job — this
module is that proof-of-life loop.

The contract is **best-effort, never fatal**:

* A single ``update_heartbeat`` failure (a transient DB hiccup) is **logged and
  swallowed** — a missed beat, never an exception that escapes the loop or kills
  the worker. The 60s reaper threshold tolerates ~5 missed 10s beats, so one bad
  beat is harmless; only a worker that is genuinely stuck/crashed (no beats for
  the whole threshold) gets reclaimed.
* :meth:`HeartbeatHandle.stop` cancels the task cleanly (idempotent) — the worker
  calls it in a ``finally`` so a beat can never leak past job completion.

Time + sleeps come from the injected :class:`~carve.runtime.clock.Clock` (the
same seam the scheduler uses), so tests drive the loop under a ``FakeClock`` with
no real sleeps. The sync ``job_queue.update_heartbeat`` is bridged off the event
loop via ``asyncio.to_thread`` exactly as ``worker.py``/``scheduler.py`` do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from carve.runtime.clock import Clock, system_clock

if TYPE_CHECKING:
    from carve.core.state.job_queue import JobQueue

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_INTERVAL_S = 10.0


class HeartbeatHandle:
    """A running heartbeat loop's handle — wraps the asyncio task + a clean stop.

    Returned by :func:`start`. The worker holds it for the duration of a job's
    execution and calls :meth:`stop` in a ``finally``. ``stop`` is idempotent and
    safe to call whether or not the loop has already finished.
    """

    def __init__(self, task: asyncio.Task[None]) -> None:
        self._task = task

    async def stop(self) -> None:
        """Cancel the heartbeat loop and await its teardown (idempotent).

        Cancels the underlying task and awaits it, suppressing the
        ``CancelledError`` that the cancellation raises — so the worker's
        ``finally`` block stays clean. A second call is a no-op (the task is
        already done).
        """
        if self._task.done():
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


async def _heartbeat_loop(
    job_queue: JobQueue,
    job_id: str,
    *,
    interval_s: float,
    clock: Clock,
    worker_id: str | None,
) -> None:
    """Stamp ``heartbeat_at`` for ``job_id`` every ``interval_s`` until cancelled.

    Each beat bridges the sync ``update_heartbeat`` off the event loop. A beat
    that raises (a transient DB failure) is logged and swallowed so the loop
    survives — best-effort proof-of-life, never a crash. The loop only exits on
    cancellation (the ``CancelledError`` propagates out of the sleep, ending the
    task); :meth:`HeartbeatHandle.stop` drives that. ``worker_id`` scopes the beat
    to this owner (a returning zombie's beat no-ops on a reclaimed job).
    """
    while True:
        try:
            await asyncio.to_thread(
                job_queue.update_heartbeat,
                job_id,
                now=clock.now(),
                expected_worker_id=worker_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A missed beat must never kill the loop or the worker — log + carry
            # on. The reaper's threshold tolerates several missed beats.
            logger.warning("heartbeat for job %s failed; will retry next beat", job_id)
        await clock.sleep_until_next_boundary(interval_s)


def start(
    job_queue: JobQueue,
    job_id: str,
    *,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    clock: Clock = system_clock,
    worker_id: str | None = None,
) -> HeartbeatHandle:
    """Start a best-effort heartbeat loop for ``job_id``; return its handle.

    Schedules :func:`_heartbeat_loop` as an asyncio task on the running loop and
    wraps it in a :class:`HeartbeatHandle`. The caller (the worker) stops it in a
    ``finally`` once the job finishes. The claim already stamps the initial
    ``heartbeat_at``; this loop keeps it fresh. ``worker_id`` makes each beat
    ownership-aware (uniform with the worker's other writes).
    """
    task = asyncio.create_task(
        _heartbeat_loop(job_queue, job_id, interval_s=interval_s, clock=clock, worker_id=worker_id)
    )
    return HeartbeatHandle(task)


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "HeartbeatHandle",
    "start",
]
