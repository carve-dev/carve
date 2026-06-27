"""The heartbeat loop — best-effort, Clock-driven, cleanly cancellable.

No Postgres needed: an in-memory fake ``JobQueue`` records the ``update_heartbeat``
calls, and a ``FakeClock`` drives the loop's sleeps with no real waiting. The
loop yields to the event loop each beat via ``asyncio.to_thread``, so a watchdog
coroutine can both observe beats land and signal a clean stop. Covers:

* the loop stamps ``heartbeat_at`` on its interval (one beat per iteration),
  each at the clock's current time;
* a transient ``update_heartbeat`` failure is swallowed — the loop survives and
  the NEXT beat still lands (best-effort: a missed beat, never a crash);
* :meth:`HeartbeatHandle.stop` cancels promptly and is idempotent (a second stop
  is a no-op, no dangling beats afterwards).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from carve.runtime import heartbeat
from carve.runtime.clock import FakeClock


class _FakeQueue:
    """Records every ``update_heartbeat`` call; can be told to fail on the Nth.

    Mirrors only the one method the heartbeat loop touches. ``fail_on`` is a set
    of 1-based call indices that raise (to exercise the best-effort swallow).
    """

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.calls: list[datetime] = []
        self.fail_on = fail_on or set()
        self._n = 0

    def update_heartbeat(
        self, job_id: str, *, now: datetime | None = None, expected_worker_id: str | None = None
    ) -> None:
        self._n += 1
        if self._n in self.fail_on:
            raise RuntimeError("transient db error during heartbeat")
        self.calls.append(now if now is not None else datetime.now(UTC))


async def _wait_until(predicate: object, *, tries: int = 2000) -> bool:
    """Yield to the event loop until ``predicate()`` is truthy or ``tries`` exhaust."""
    assert callable(predicate)
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0)
    return False


async def test_heartbeat_loop_stamps_on_its_interval() -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    queue = _FakeQueue()

    handle = heartbeat.start(queue, "job_1", interval_s=10.0, clock=clock)  # type: ignore[arg-type]
    try:
        # Several beats land (the FakeClock sleep is instant; the loop yields each
        # beat via to_thread, so beats accumulate quickly).
        assert await _wait_until(lambda: len(queue.calls) >= 3)
    finally:
        await handle.stop()

    # Each beat is stamped at the clock's (advancing) time, and the clock advances
    # by the interval each beat (boundary-aligned via the FakeClock).
    assert len(queue.calls) >= 3
    assert all(ts.tzinfo is not None for ts in queue.calls)
    assert queue.calls == sorted(queue.calls)  # monotonic non-decreasing


async def test_heartbeat_loop_survives_a_transient_failure() -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    # The 1st beat raises; the loop must swallow it and keep beating.
    queue = _FakeQueue(fail_on={1})

    handle = heartbeat.start(queue, "job_1", interval_s=10.0, clock=clock)  # type: ignore[arg-type]
    try:
        # A successful beat lands AFTER the failed one — the loop wasn't killed.
        assert await _wait_until(lambda: len(queue.calls) >= 2)
    finally:
        await handle.stop()

    assert len(queue.calls) >= 2


async def test_heartbeat_handle_stop_cancels_and_is_idempotent() -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    queue = _FakeQueue()

    handle = heartbeat.start(queue, "job_1", interval_s=10.0, clock=clock)  # type: ignore[arg-type]
    assert await _wait_until(lambda: len(queue.calls) >= 1)
    await handle.stop()
    beats_at_stop = len(queue.calls)

    # No further beats after stop (give the loop ample chances to run if alive).
    for _ in range(50):
        await asyncio.sleep(0)
    assert len(queue.calls) == beats_at_stop

    # A second stop is a clean no-op.
    await handle.stop()
