"""Timezone-aware cron math — the scheduler's correctness core.

A schedule's ``cron`` is evaluated **in its own ``timezone``** (a
``zoneinfo.ZoneInfo``), so a ``0 2 * * *`` schedule means "2am local" and stays
correct across DST transitions: croniter, given a tz-aware datetime in that zone,
never fires a wall-clock time twice on spring-forward nor skips one on fall-back.
Every returned instant is converted to **UTC-aware** for storage in
``schedules.next_fires_at``/``last_fired_at`` (``TIMESTAMPTZ``) and for
comparison against an injected ``Clock``'s UTC ``now``.

Two functions, both pure and well-unit-tested:

* :func:`next_tick_after` — the tick **strictly after** ``after`` (what a fire
  advances ``next_fires_at`` to, and the initial ``next_fires_at`` at seed).
* :func:`this_tick_at` — the canonical tick **at or before** ``now`` (the cron
  window's "scheduled_for" instant, stamped onto the enqueued job).

:func:`is_valid_cron` wraps ``croniter.is_valid`` so the CLI can reject a bad
expression up front without importing croniter itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter


class CronError(ValueError):
    """Raised when a cron expression or timezone name is invalid.

    The CLI validates up front and exits 2; the repository raises this if a
    stored row somehow carries a bad value (it cannot, given the CLI gate, but
    the cron module never trusts its input).
    """


def is_valid_cron(expr: str) -> bool:
    """Return whether ``expr`` is a valid cron expression (croniter's grammar)."""
    return bool(croniter.is_valid(expr))


def is_valid_timezone(name: str) -> bool:
    """Return whether ``name`` resolves to a ``zoneinfo`` timezone."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"unknown timezone {timezone!r}") from exc


def _validated_iter(cron: str, start: datetime) -> croniter:
    if not croniter.is_valid(cron):
        raise CronError(f"invalid cron expression {cron!r}")
    return croniter(cron, start)


def next_tick_after(cron: str, after: datetime, timezone: str = "UTC") -> datetime:
    """The next cron tick **strictly after** ``after``, as a UTC-aware datetime.

    ``cron`` is evaluated in ``timezone`` (DST-correct). ``after`` may be naive
    (assumed UTC) or aware; it is normalized into ``timezone`` for the croniter
    computation, and the result is returned converted to UTC.

    This is what a fire advances ``next_fires_at`` to (``set_last_fired`` passes
    the just-fired tick), and what ``seed`` uses to compute the initial
    ``next_fires_at`` (passing the seed instant). croniter's ``get_next`` from an
    instant that *is* a tick returns the FOLLOWING tick, which is exactly the
    advance semantics we want.
    """
    zone = _zone(timezone)
    local_after = _as_aware_utc(after).astimezone(zone)
    try:
        nxt: datetime = _validated_iter(cron, local_after).get_next(datetime)
    except CroniterBadDateError as exc:
        # Grammatically valid but UNSATISFIABLE (e.g. `0 0 30 2 *` = Feb 30):
        # croniter exhausts its search and raises. Surface the typed CronError
        # (the CLI maps it to exit 2; the scheduler loop's per-pass guard skips
        # it) rather than leaking a raw croniter traceback.
        raise CronError(f"cron {cron!r} has no reachable run time (unsatisfiable)") from exc
    return nxt.astimezone(UTC)


def this_tick_at(cron: str, now: datetime, timezone: str = "UTC") -> datetime:
    """The canonical cron tick **at or before** ``now``, as a UTC-aware datetime.

    This is the cron window's instant: when the scheduler fires a due schedule it
    stamps the enqueued job's ``scheduled_for`` with this tick (so two ticks in
    the same window enqueue the *same* ``scheduled_for`` and the dedup index sees
    one window). If ``now`` lands exactly on a tick, that tick is returned
    (inclusive). ``cron`` is evaluated in ``timezone``; the result is UTC-aware.
    """
    zone = _zone(timezone)
    local_now = _as_aware_utc(now).astimezone(zone)
    # croniter's ``get_prev`` from an exact tick returns the *previous* tick, so
    # to make the boundary inclusive we step one microsecond forward first: then
    # ``get_prev`` returns the tick at-or-before the original ``now``.
    inclusive = local_now + timedelta(microseconds=1)
    try:
        prev: datetime = _validated_iter(cron, inclusive).get_prev(datetime)
    except CroniterBadDateError as exc:
        raise CronError(f"cron {cron!r} has no reachable run time (unsatisfiable)") from exc
    return prev.astimezone(UTC)


def _as_aware_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC; pass an aware one through unchanged."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


__all__ = [
    "CronError",
    "is_valid_cron",
    "is_valid_timezone",
    "next_tick_after",
    "this_tick_at",
]
