"""The scheduler loop — deterministic, dedup-safe, boundary-aligned.

Postgres-fixture-gated where it touches the real ``schedules``/``jobs`` tables +
the partial indexes. Covers:

* A due schedule enqueues exactly once per cron window (dedup-safe: a 2nd pass at
  the same ``now`` hits ``QueuedJobAlreadyExists`` → ``schedule.skipped``, never a
  double-enqueue).
* ``set_last_fired`` advances ``next_fires_at`` so the same window doesn't re-fire
  on the next pass.
* A paused schedule is skipped.
* The async ``scheduler_loop`` is deterministic under a ``FakeClock`` (no real
  sleeps), fires at the cron tick, sleeps boundary-aligned, and stops on shutdown.
* The ``_emit`` seam is invoked with the right kind/payload (the locked contract).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.core.state.schedules import Schedules
from carve.runtime.clock import FakeClock, _seconds_to_next_boundary
from carve.runtime.scheduler import run_due_once, scheduler_loop


@pytest.fixture
def factories(
    postgres_state_store_url: str,
) -> tuple[Schedules, JobQueue, list[tuple[str, dict[str, Any]]]]:
    config = Config(
        project=ProjectConfig(name="sched-loop-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    schedules = Schedules(session_factory)
    job_queue = JobQueue(session_factory)

    # Spy on the _emit seam — the contract is "invoked with the right kind/
    # payload" (Decision 2: a no-op seam, asserted at the seam, not persistence).
    emitted: list[tuple[str, dict[str, Any]]] = []
    original = schedules._emit

    def spy(kind: str, payload: dict[str, Any]) -> None:
        emitted.append((kind, payload))
        original(kind, payload)

    schedules._emit = spy  # type: ignore[method-assign]
    return schedules, job_queue, emitted


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _force_next_fires_at(schedules: Schedules, schedule_id: str, value: datetime) -> None:
    from carve.core.state.models import Schedule

    with schedules._session_factory() as session:
        sched = session.get(Schedule, schedule_id)
        assert sched is not None
        sched.next_fires_at = value
        session.commit()


def _queued_count(job_queue: JobQueue, pipeline: str) -> int:
    """Count queued jobs for a pipeline (proves the no-double-enqueue invariant)."""
    import sqlalchemy as sa

    from carve.core.state.models import Job

    stmt = sa.select(sa.func.count()).where(Job.pipeline == pipeline, Job.status == "queued")
    with job_queue._session_factory() as session:
        return int(session.scalar(stmt) or 0)


# ------------------------------------------------------------- run_due_once


def test_run_due_once_fires_a_due_schedule_exactly_once(
    factories: tuple[Schedules, JobQueue, list[tuple[str, dict[str, Any]]]],
) -> None:
    schedules, job_queue, emitted = factories
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    _force_next_fires_at(schedules, sched.id, _utc(2026, 1, 1, 12, 5))

    now = _utc(2026, 1, 1, 12, 5)
    fired = run_due_once(schedules, job_queue, now)
    assert fired == 1

    # Exactly one queued job exists, stamped with the canonical cron tick.
    assert _queued_count(job_queue, "sales") == 1
    fired_events = [e for e in emitted if e[0] == "schedule.fired"]
    assert len(fired_events) == 1
    assert fired_events[0][1]["pipeline"] == "sales"
    assert fired_events[0][1]["scheduled_for"] == _utc(2026, 1, 1, 12, 5).isoformat()


def test_second_pass_same_window_skips_no_double_enqueue(
    factories: tuple[Schedules, JobQueue, list[tuple[str, dict[str, Any]]]],
) -> None:
    schedules, job_queue, emitted = factories
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    _force_next_fires_at(schedules, sched.id, _utc(2026, 1, 1, 12, 5))

    now = _utc(2026, 1, 1, 12, 5)
    run_due_once(schedules, job_queue, now)
    # After the fire, next_fires_at advanced to 12:10 — so the same `now` is no
    # longer due. Force it back into the window to simulate a 2nd pass colliding
    # with the still-queued job (the dedup path).
    _force_next_fires_at(schedules, sched.id, _utc(2026, 1, 1, 12, 5))
    fired = run_due_once(schedules, job_queue, now)
    assert fired == 0  # the 2nd pass enqueued nothing
    assert _queued_count(job_queue, "sales") == 1  # NEVER a double-enqueue

    skipped = [e for e in emitted if e[0] == "schedule.skipped"]
    assert len(skipped) == 1
    assert skipped[0][1]["pipeline"] == "sales"
    assert skipped[0][1]["reason"] == "queued_job_already_exists"


def test_fire_advances_next_fires_at_so_it_does_not_refire(
    factories: tuple[Schedules, JobQueue, list[tuple[str, dict[str, Any]]]],
) -> None:
    schedules, job_queue, _ = factories
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    _force_next_fires_at(schedules, sched.id, _utc(2026, 1, 1, 12, 5))

    now = _utc(2026, 1, 1, 12, 5)
    run_due_once(schedules, job_queue, now)
    refreshed = schedules.get("sales")
    assert refreshed is not None
    assert refreshed.next_fires_at == _utc(2026, 1, 1, 12, 10)
    # A 2nd pass at the same `now` sees nothing due (the advance, not the dedup,
    # is what stops the re-fire).
    assert run_due_once(schedules, job_queue, now) == 0


def test_paused_schedule_is_skipped(
    factories: tuple[Schedules, JobQueue, list[tuple[str, dict[str, Any]]]],
) -> None:
    schedules, job_queue, emitted = factories
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    _force_next_fires_at(schedules, sched.id, _utc(2026, 1, 1, 12, 5))
    schedules.pause("sales")

    fired = run_due_once(schedules, job_queue, _utc(2026, 1, 1, 12, 5))
    assert fired == 0
    assert not [e for e in emitted if e[0] == "schedule.fired"]


def test_missed_ticks_clock_jump_produces_one_fire(
    factories: tuple[Schedules, JobQueue, list[tuple[str, dict[str, Any]]]],
) -> None:
    # Clock jumps forward 20 minutes past a */5 schedule whose next_fires_at is in
    # the past: one pass produces ONE fire (the queued-job dedup makes a backlog
    # coalesce into a single queued job), not four.
    schedules, job_queue, emitted = factories
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    _force_next_fires_at(schedules, sched.id, _utc(2026, 1, 1, 12, 0))

    now = _utc(2026, 1, 1, 12, 20)
    fired = run_due_once(schedules, job_queue, now)
    assert fired == 1
    assert len([e for e in emitted if e[0] == "schedule.fired"]) == 1


# ----------------------------------------------------- async scheduler_loop


async def test_scheduler_loop_is_deterministic_under_fake_clock(
    factories: tuple[Schedules, JobQueue, list[tuple[str, dict[str, Any]]]],
) -> None:
    schedules, job_queue, emitted = factories
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    # next_fires_at exactly on a tick the fake clock starts at.
    _force_next_fires_at(schedules, sched.id, _utc(2026, 1, 1, 12, 5))

    clock = FakeClock(_utc(2026, 1, 1, 12, 5))
    shutdown = asyncio.Event()

    async def stop_after_first_fire() -> None:
        # Poll until the first fire lands, then signal shutdown.
        for _ in range(200):
            if any(e[0] == "schedule.fired" for e in emitted):
                shutdown.set()
                return
            await asyncio.sleep(0)
        shutdown.set()

    await asyncio.gather(
        scheduler_loop(schedules, job_queue, interval_s=30.0, clock=clock, shutdown=shutdown),
        stop_after_first_fire(),
    )

    fired = [e for e in emitted if e[0] == "schedule.fired"]
    assert len(fired) >= 1
    assert fired[0][1]["pipeline"] == "sales"


async def test_scheduler_loop_sleeps_boundary_aligned_between_passes(
    factories: tuple[Schedules, JobQueue, list[tuple[str, dict[str, Any]]]],
) -> None:
    # With no due schedule, the loop makes empty passes and sleeps to the next
    # 30s wall-clock boundary each time (via the FakeClock, no real sleep). Stop
    # after two passes and assert the recorded sleep is boundary-aligned, not
    # ``now + interval``.
    schedules, job_queue, _ = factories
    clock = FakeClock(_utc(2026, 1, 1, 12, 0, 7))  # 7s into a minute
    shutdown = asyncio.Event()

    async def stop_after_two_sleeps() -> None:
        for _ in range(500):
            if len(clock.slept_for) >= 2:
                shutdown.set()
                return
            await asyncio.sleep(0)
        shutdown.set()

    await asyncio.gather(
        scheduler_loop(schedules, job_queue, interval_s=30.0, clock=clock, shutdown=shutdown),
        stop_after_two_sleeps(),
    )

    assert clock.slept_for
    # From :07 the next 30s boundary is :30 -> 23s, not a full 30s interval.
    assert clock.slept_for[0] == _seconds_to_next_boundary(_utc(2026, 1, 1, 12, 0, 7), 30.0)
    assert clock.slept_for[0] == pytest.approx(23.0)


async def test_scheduler_loop_stops_on_shutdown_without_firing(
    factories: tuple[Schedules, JobQueue, list[tuple[str, dict[str, Any]]]],
) -> None:
    schedules, job_queue, emitted = factories
    # No due schedule — the loop should make one empty pass then exit on shutdown.
    clock = FakeClock(_utc(2026, 1, 1, 12, 0))
    shutdown = asyncio.Event()
    shutdown.set()  # pre-set: the loop exits after at most one pass

    await asyncio.wait_for(
        scheduler_loop(schedules, job_queue, interval_s=30.0, clock=clock, shutdown=shutdown),
        timeout=2.0,
    )
    assert not [e for e in emitted if e[0] == "schedule.fired"]
