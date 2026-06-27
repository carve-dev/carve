"""Timezone-aware cron math — pure unit tests (no Postgres).

Covers ``next_tick_after`` / ``this_tick_at`` correctness for ``*/5`` and daily
crons, the strictly-after / inclusive-at-or-before boundary semantics, a non-UTC
timezone, a **DST spring-forward boundary** (the trap), and the bad-cron /
bad-timezone error paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from carve.runtime.cron import (
    CronError,
    is_valid_cron,
    is_valid_timezone,
    next_tick_after,
    this_tick_at,
)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_next_tick_after_every_five_minutes() -> None:
    nxt = next_tick_after("*/5 * * * *", _utc(2026, 1, 1, 12, 2, 30))
    assert nxt == _utc(2026, 1, 1, 12, 5)


def test_next_tick_after_is_strictly_after_an_exact_tick() -> None:
    # On an exact tick, the "next" tick is the FOLLOWING one — this is the
    # advance semantics set_last_fired relies on.
    nxt = next_tick_after("*/5 * * * *", _utc(2026, 1, 1, 12, 5))
    assert nxt == _utc(2026, 1, 1, 12, 10)


def test_unsatisfiable_cron_raises_cron_error_not_croniter_traceback() -> None:
    # `0 0 30 2 *` = Feb 30: grammatically valid (passes is_valid_cron) but
    # never matches a real date. croniter exhausts its search and raises
    # CroniterBadDateError; we surface the typed CronError instead of leaking a
    # raw traceback (the CLI maps it to exit 2; the loop's per-pass guard skips).
    assert is_valid_cron("0 0 30 2 *") is True
    with pytest.raises(CronError, match="unsatisfiable"):
        next_tick_after("0 0 30 2 *", _utc(2026, 1, 1, 12, 0))
    with pytest.raises(CronError, match="unsatisfiable"):
        this_tick_at("0 0 30 2 *", _utc(2026, 1, 1, 12, 0))


def test_this_tick_at_is_inclusive_on_an_exact_tick() -> None:
    tick = this_tick_at("*/5 * * * *", _utc(2026, 1, 1, 12, 5))
    assert tick == _utc(2026, 1, 1, 12, 5)


def test_this_tick_at_returns_the_window_tick_when_mid_window() -> None:
    tick = this_tick_at("*/5 * * * *", _utc(2026, 1, 1, 12, 7, 30))
    assert tick == _utc(2026, 1, 1, 12, 5)


def test_results_are_utc_aware() -> None:
    nxt = next_tick_after("0 * * * *", _utc(2026, 1, 1, 12, 30))
    assert nxt.tzinfo is not None
    assert nxt.utcoffset() == datetime(2026, 1, 1, tzinfo=UTC).utcoffset()


def test_naive_after_is_treated_as_utc() -> None:
    naive = datetime(2026, 1, 1, 12, 2, 0)
    assert next_tick_after("*/5 * * * *", naive) == _utc(2026, 1, 1, 12, 5)


def test_cron_evaluated_in_schedule_timezone() -> None:
    # ``0 2 * * *`` means 2am *local*. In America/New_York (UTC-5 in January),
    # 2am EST is 07:00 UTC.
    nxt = next_tick_after("0 2 * * *", _utc(2026, 1, 15, 10, 0), timezone="America/New_York")
    assert nxt == _utc(2026, 1, 16, 7, 0)


def test_dst_spring_forward_does_not_double_fire_or_skip() -> None:
    # 2026-03-08 is US spring-forward: clocks jump 02:00 -> 03:00 EST->EDT, so
    # ``0 2 * * *`` has no 2am that day. croniter advances to 03:00 EDT (07:00
    # UTC) — it neither fires twice nor skips the day.
    tz = "America/New_York"

    # From mid-day 3/7 (2am already past), the next fire is 3/8 — where 2am
    # doesn't exist, so it lands on 03:00 EDT = 07:00 UTC (advanced correctly,
    # not skipped).
    boundary_fire = next_tick_after("0 2 * * *", _utc(2026, 3, 7, 12, 0), timezone=tz)
    assert boundary_fire == _utc(2026, 3, 8, 7, 0)

    # 3/7 itself is a normal day: 2am EST = 07:00 UTC (one fire, from the night
    # before).
    seventh_fire = next_tick_after("0 2 * * *", _utc(2026, 3, 6, 12, 0), timezone=tz)
    assert seventh_fire == _utc(2026, 3, 7, 7, 0)

    # The day after the boundary is a normal 2am EDT = 06:00 UTC — exactly one
    # fire per day across the transition (no double-fire, no skip).
    third = next_tick_after("0 2 * * *", boundary_fire, timezone=tz)
    assert third == _utc(2026, 3, 9, 6, 0)


def test_dst_fall_back_fires_once() -> None:
    # 2026-11-01 is US fall-back: 02:00 EDT -> 01:00 EST (01:00 occurs twice).
    # ``0 2 * * *`` fires once at 2am EST = 07:00 UTC.
    nxt = next_tick_after("0 2 * * *", _utc(2026, 11, 1, 0, 0), timezone="America/New_York")
    assert nxt == _utc(2026, 11, 1, 7, 0)


def test_bad_cron_raises_cron_error() -> None:
    with pytest.raises(CronError):
        next_tick_after("not a cron", _utc(2026, 1, 1, 0, 0))
    with pytest.raises(CronError):
        this_tick_at("99 99 * * *", _utc(2026, 1, 1, 0, 0))


def test_bad_timezone_raises_cron_error() -> None:
    with pytest.raises(CronError):
        next_tick_after("0 2 * * *", _utc(2026, 1, 1, 0, 0), timezone="Mars/Olympus")


def test_is_valid_cron_and_timezone() -> None:
    assert is_valid_cron("*/5 * * * *")
    assert not is_valid_cron("nope")
    assert is_valid_timezone("America/New_York")
    assert is_valid_timezone("UTC")
    assert not is_valid_timezone("Mars/Olympus")


def test_zoneinfo_round_trip_matches_manual_conversion() -> None:
    # A sanity cross-check that the module's UTC conversion equals a manual one.
    tz = ZoneInfo("Europe/London")
    nxt = next_tick_after("30 9 * * *", _utc(2026, 7, 1, 0, 0), timezone="Europe/London")
    # 1 July: London is BST (UTC+1), so 09:30 local = 08:30 UTC.
    manual = datetime(2026, 7, 1, 9, 30, tzinfo=tz).astimezone(UTC)
    assert nxt == manual
