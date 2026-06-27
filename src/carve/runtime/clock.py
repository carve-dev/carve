"""The ``Clock`` seam — makes the scheduler loop deterministic + sleep-free in tests.

The scheduler must never call ``datetime.now()`` or ``time.sleep`` directly: all
time comes from an injected :class:`Clock` so a test can drive the loop with a
:class:`FakeClock` (set/advance, instant sleeps) and assert fires land at exact
cron ticks. Production uses :data:`system_clock` (real UTC ``now()`` + a real
boundary-aligned async sleep).

The loop sleeps to the **next wall-clock interval boundary** (not ``now +
interval``) so a ``*/5 * * * *`` schedule stays aligned to :00/:05/:10 regardless
of how long a poll took — :meth:`Clock.sleep_until_next_boundary` computes the
delay from ``now()`` and the loop's ``interval_s``.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """The time seam the scheduler loop depends on.

    ``now()`` returns a **UTC-aware** datetime. ``sleep_until_next_boundary``
    awaits until the next ``interval_s`` wall-clock boundary (e.g. for
    ``interval_s=30`` it wakes at :00 and :30 of each minute), computed from
    ``now()`` — so a slow poll doesn't drift the schedule.
    """

    def now(self) -> datetime:
        """Return the current instant as a UTC-aware datetime."""
        ...

    async def sleep_until_next_boundary(self, interval_s: float) -> None:
        """Await until the next ``interval_s`` wall-clock boundary."""
        ...


def _seconds_to_next_boundary(now: datetime, interval_s: float) -> float:
    """Seconds from ``now`` to the next ``interval_s`` epoch-aligned boundary.

    Boundaries are multiples of ``interval_s`` since the Unix epoch, so a
    ``*/5``-minute schedule with ``interval_s=30`` keeps waking on the same
    :00/:30 grid every minute. If ``now`` lands exactly on a boundary the next
    one is a full ``interval_s`` away (never a zero-length sleep that spins).
    """
    if interval_s <= 0:
        return 0.0
    epoch_s = now.timestamp()
    next_boundary = (math.floor(epoch_s / interval_s) + 1) * interval_s
    return next_boundary - epoch_s


class SystemClock:
    """The real clock: UTC ``now()`` + a real boundary-aligned ``asyncio.sleep``."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep_until_next_boundary(self, interval_s: float) -> None:
        await asyncio.sleep(_seconds_to_next_boundary(self.now(), interval_s))


# The production default — a module-level singleton (it carries no state).
system_clock: Clock = SystemClock()


class FakeClock:
    """A deterministic clock for tests: set/advance time, instant sleeps.

    ``now()`` returns a fixed instant until :meth:`set` or :meth:`advance` moves
    it. :meth:`sleep_until_next_boundary` advances the clock to the next boundary
    **without any real sleep** and records the slept duration in
    :attr:`slept_for`, so a test can both assert the loop is boundary-aligned and
    run it to completion in microseconds.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        self._now = start.astimezone(UTC)
        self.slept_for: list[float] = []

    def now(self) -> datetime:
        return self._now

    def set(self, instant: datetime) -> None:
        """Jump the clock to ``instant`` (normalized to UTC-aware)."""
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        self._now = instant.astimezone(UTC)

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)

    async def sleep_until_next_boundary(self, interval_s: float) -> None:
        """Advance to the next boundary instantly; record the duration slept."""
        delay = _seconds_to_next_boundary(self._now, interval_s)
        self.slept_for.append(delay)
        self.advance(delay)


__all__ = [
    "Clock",
    "FakeClock",
    "SystemClock",
    "system_clock",
]
