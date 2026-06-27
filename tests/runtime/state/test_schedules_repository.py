"""The schedules repository — seed/upsert, list_due, set_last_fired, live mutators.

Postgres-fixture-gated (the partial ``ix_schedules_due`` index + the
``ck_schedules_pause_origin`` CHECK don't exist in SQLite). Mirrors
``test_job_queue_enqueue.py``'s fixture block. Covers:

* ``seed`` creates a row with a correct initial ``next_fires_at`` and upserts.
* ``list_due`` returns only due + unpaused rows.
* ``set_last_fired`` advances ``next_fires_at`` to the FOLLOWING tick (the
  load-bearing recompute) so the row leaves the due window.
* ``pause``/``resume``/``set_cron`` mutate the row, append a ``schedule_changes``
  audit row, and (set_cron) recompute ``next_fires_at``.
* The ``ck_schedules_pause_origin`` CHECK rejects inconsistent pause states.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.schedules import ScheduleNotFound, Schedules


@pytest.fixture
def schedules(postgres_state_store_url: str) -> Schedules:
    config = Config(
        project=ProjectConfig(name="sched-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return Schedules(create_session_factory(engine))


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --------------------------------------------------------------------- seed


def test_seed_creates_row_with_initial_next_fires_at(schedules: Schedules) -> None:
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    assert sched.pipeline == "sales"
    assert sched.cron == "*/5 * * * *"
    assert sched.target == "dev"
    assert sched.timezone == "UTC"
    assert sched.paused is False
    assert sched.paused_by is None
    assert sched.id.startswith("sched_")
    # next_fires_at is set to the next tick strictly after seed time (future).
    assert sched.next_fires_at is not None
    assert sched.next_fires_at > datetime.now(UTC) - timedelta(minutes=5)


def test_seed_is_idempotent_upsert(schedules: Schedules) -> None:
    first = schedules.seed("sales", "*/5 * * * *", "dev")
    second = schedules.seed("sales", "0 2 * * *", "prod", timezone="America/New_York")
    assert second.id == first.id  # same row, updated in place
    assert second.cron == "0 2 * * *"
    assert second.target == "prod"
    assert second.timezone == "America/New_York"
    # Both seeds wrote an audit row (create + reseed).
    changes = schedules.list_changes("sales")
    assert len(changes) == 2
    assert {c.change_kind for c in changes} == {"set_cron", "reseed"}
    assert all(c.source == "seed" for c in changes)


# ----------------------------------------------------------------- list_due


def test_list_due_returns_only_due_unpaused_rows(schedules: Schedules) -> None:
    due = schedules.seed("a", "*/5 * * * *", "dev")
    schedules.seed("b", "*/5 * * * *", "dev")
    paused = schedules.seed("c", "*/5 * * * *", "dev")
    schedules.pause("c")

    # Force `a`'s next_fires_at into the past so it is due; leave `b` future.
    _force_next_fires_at(schedules, due.id, _utc(2020, 1, 1, 0, 0))
    _force_next_fires_at(schedules, paused.id, _utc(2020, 1, 1, 0, 0))

    now = _utc(2020, 1, 1, 0, 5)
    rows = schedules.list_due(now)
    names = {s.pipeline for s in rows}
    assert names == {"a"}  # b not yet due, c paused


def test_list_due_excludes_null_next_fires_at(schedules: Schedules) -> None:
    sched = schedules.seed("a", "*/5 * * * *", "dev")
    _force_next_fires_at(schedules, sched.id, None)
    assert schedules.list_due(datetime.now(UTC)) == []


# ----------------------------------------------------------- set_last_fired


def test_set_last_fired_advances_next_fires_at_to_following_tick(
    schedules: Schedules,
) -> None:
    sched = schedules.seed("a", "*/5 * * * *", "dev")
    # Pin next_fires_at to a known tick, then fire "at" that tick.
    _force_next_fires_at(schedules, sched.id, _utc(2026, 1, 1, 12, 5))
    fired = schedules.set_last_fired(sched.id, _utc(2026, 1, 1, 12, 5))
    assert fired.last_fired_at == _utc(2026, 1, 1, 12, 5)
    # The load-bearing advance: next tick after the just-fired 12:05 is 12:10.
    assert fired.next_fires_at == _utc(2026, 1, 1, 12, 10)


def test_set_last_fired_leaves_row_not_due_after_fire(schedules: Schedules) -> None:
    sched = schedules.seed("a", "*/5 * * * *", "dev")
    _force_next_fires_at(schedules, sched.id, _utc(2026, 1, 1, 12, 5))
    now = _utc(2026, 1, 1, 12, 5)
    assert {s.pipeline for s in schedules.list_due(now)} == {"a"}
    schedules.set_last_fired(sched.id, now)
    # After the advance, the same `now` no longer sees the row as due.
    assert schedules.list_due(now) == []


def test_set_last_fired_unknown_id_raises(schedules: Schedules) -> None:
    with pytest.raises(ScheduleNotFound):
        schedules.set_last_fired("sched_missing", datetime.now(UTC))


# ------------------------------------------------------------- live mutators


def test_pause_sets_origin_and_appends_audit(schedules: Schedules) -> None:
    schedules.seed("a", "*/5 * * * *", "dev")
    paused = schedules.pause("a", reason="maintenance")
    assert paused.paused is True
    assert paused.paused_by == "user"
    assert paused.pause_reason == "maintenance"

    changes = schedules.list_changes("a")
    pause_changes = [c for c in changes if c.change_kind == "pause"]
    assert len(pause_changes) == 1
    change = pause_changes[0]
    assert change.source == "cli"
    assert change.reason == "maintenance"
    assert change.actor_token_id is None
    assert change.before["paused"] is False
    assert change.after["paused"] is True


def test_resume_clears_origin_and_appends_audit(schedules: Schedules) -> None:
    schedules.seed("a", "*/5 * * * *", "dev")
    schedules.pause("a")
    resumed = schedules.resume("a", reason="done")
    assert resumed.paused is False
    assert resumed.paused_by is None
    assert resumed.pause_reason is None
    resume_changes = [c for c in schedules.list_changes("a") if c.change_kind == "resume"]
    assert len(resume_changes) == 1
    assert resume_changes[0].reason == "done"


def test_set_cron_recomputes_next_fires_at_and_audits(schedules: Schedules) -> None:
    sched = schedules.seed("a", "0 0 * * *", "dev")
    before_next = sched.next_fires_at
    updated = schedules.set_cron("a", "*/5 * * * *", reason="more often")
    assert updated.cron == "*/5 * * * *"
    # next_fires_at recomputed (a 5-min cron fires much sooner than midnight).
    assert updated.next_fires_at != before_next
    assert updated.next_fires_at is not None

    set_cron_changes = [c for c in schedules.list_changes("a") if c.change_kind == "set_cron"]
    # One from the seed (create), one from this set_cron.
    cli_change = [c for c in set_cron_changes if c.source == "cli"]
    assert len(cli_change) == 1
    assert cli_change[0].before["cron"] == "0 0 * * *"
    assert cli_change[0].after["cron"] == "*/5 * * * *"
    assert cli_change[0].reason == "more often"


def test_set_cron_upserts_when_absent(schedules: Schedules) -> None:
    # No seed first — set_cron must create the row (standalone, no reconciler).
    created = schedules.set_cron("fresh", "*/10 * * * *", target="prod")
    assert created.pipeline == "fresh"
    assert created.cron == "*/10 * * * *"
    assert created.target == "prod"
    assert created.next_fires_at is not None
    assert schedules.get("fresh") is not None


def test_set_cron_with_timezone_recomputes(schedules: Schedules) -> None:
    schedules.seed("a", "0 2 * * *", "dev")
    updated = schedules.set_cron("a", "0 2 * * *", timezone="America/New_York")
    assert updated.timezone == "America/New_York"


def test_pause_unknown_pipeline_raises(schedules: Schedules) -> None:
    with pytest.raises(ScheduleNotFound):
        schedules.pause("nope")
    with pytest.raises(ScheduleNotFound):
        schedules.resume("nope")


# ------------------------------------------------------- the CHECK constraint


def test_check_rejects_paused_with_null_origin(schedules: Schedules) -> None:
    """ck_schedules_pause_origin: a paused row with NULL paused_by is rejected."""
    sched = schedules.seed("a", "*/5 * * * *", "dev")
    with pytest.raises(IntegrityError):
        _raw_update(schedules, sched.id, paused=True, paused_by=None)


def test_check_rejects_active_with_non_null_origin(schedules: Schedules) -> None:
    """ck_schedules_pause_origin: an active row with a non-NULL paused_by is rejected."""
    sched = schedules.seed("a", "*/5 * * * *", "dev")
    with pytest.raises(IntegrityError):
        _raw_update(schedules, sched.id, paused=False, paused_by="user")


def test_check_accepts_recovery_origin(schedules: Schedules) -> None:
    """The CHECK allows origin='recovery' (the deferred recovery slice's value)."""
    sched = schedules.seed("a", "*/5 * * * *", "dev")
    # A direct write with a valid recovery-origin pause is accepted by the CHECK.
    _raw_update(schedules, sched.id, paused=True, paused_by="recovery")
    refreshed = schedules.get("a")
    assert refreshed is not None
    assert refreshed.paused_by == "recovery"


# ------------------------------------------------------------------ helpers


def _force_next_fires_at(schedules: Schedules, schedule_id: str, value: datetime | None) -> None:
    """Directly set ``next_fires_at`` (test scaffolding to control the due window)."""
    from carve.core.state.models import Schedule

    with schedules._session_factory() as session:
        sched = session.get(Schedule, schedule_id)
        assert sched is not None
        sched.next_fires_at = value
        session.commit()


def _raw_update(schedules: Schedules, schedule_id: str, **fields: object) -> None:
    """Apply a raw row update (to provoke the CHECK constraint directly)."""
    from carve.core.state.models import Schedule

    with schedules._session_factory() as session:
        sched = session.get(Schedule, schedule_id)
        assert sched is not None
        for key, value in fields.items():
            setattr(sched, key, value)
        session.commit()
