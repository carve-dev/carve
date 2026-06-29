"""The job-queue ``_emit`` seams go live: each transition persists an event.

Postgres-fixture-gated. With an :class:`EventEmitter` injected, every queue
transition (``job.queued``/``job.claimed``/``job.reclaimed``,
``worker.registered``/``worker.unregistered``) writes a taxonomy-shaped
``events`` row. With **no** emitter the seam stays a silent no-op (back-compat) —
no rows land.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.core.state.models import Event
from carve.runtime.events import EventEmitter
from carve.runtime.reaper import reap_stale_once

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def factory(postgres_state_store_url: str) -> sessionmaker[Session]:
    config = Config(
        project=ProjectConfig(name="queue-events-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return create_session_factory(engine)


def _events(factory: sessionmaker[Session], kind: str | None = None) -> list[Event]:
    stmt = sa.select(Event).order_by(Event.id.asc())
    if kind is not None:
        stmt = stmt.where(Event.kind == kind)
    with factory() as session:
        return list(session.scalars(stmt).all())


def test_enqueue_scheduled_emits_job_queued(factory: sessionmaker[Session]) -> None:
    queue = JobQueue(factory, emitter=EventEmitter(factory))
    job = queue.enqueue_scheduled("sales", "dev", scheduled_for=datetime(2030, 1, 1, tzinfo=UTC))

    rows = _events(factory, "job.queued")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["job_id"] == job.id
    assert payload["pipeline"] == "sales"
    assert payload["target"] == "dev"
    assert payload["trigger"] == "scheduled"
    assert payload["scheduled_for"] == "2030-01-01T00:00:00+00:00"


def test_enqueue_manual_emits_job_queued(factory: sessionmaker[Session]) -> None:
    queue = JobQueue(factory, emitter=EventEmitter(factory))
    job = queue.enqueue_manual("sales", "dev", trigger="manual")

    payload = _events(factory, "job.queued")[0].payload
    assert payload["job_id"] == job.id
    assert payload["trigger"] == "manual"
    assert payload["scheduled_for"] is None


def test_claim_next_emits_job_claimed(factory: sessionmaker[Session]) -> None:
    queue = JobQueue(factory, emitter=EventEmitter(factory))
    job = queue.enqueue_manual("sales", "dev")
    queue.claim_next("worker-1")

    rows = _events(factory, "job.claimed")
    assert len(rows) == 1
    assert rows[0].payload == {"job_id": job.id, "worker_id": "worker-1"}


def test_register_and_unregister_worker_emit(factory: sessionmaker[Session]) -> None:
    queue = JobQueue(factory, emitter=EventEmitter(factory))
    queue.register_worker("w1", host="host-a", pid=4321)
    queue.unregister_worker("w1")

    registered = _events(factory, "worker.registered")
    assert len(registered) == 1
    assert registered[0].payload == {"worker_id": "w1", "host": "host-a", "pid": 4321}

    unregistered = _events(factory, "worker.unregistered")
    assert len(unregistered) == 1
    assert unregistered[0].payload == {"worker_id": "w1", "host": "host-a", "pid": 4321}


def test_reaper_emits_job_reclaimed(factory: sessionmaker[Session]) -> None:
    """The reaper's ``job.reclaimed`` rides the queue's now-live ``_emit`` seam."""
    queue = JobQueue(factory, emitter=EventEmitter(factory))
    repository = Repository(factory)
    queue.enqueue_manual("sales", "dev")
    claimed = queue.claim_next("worker-dead")
    assert claimed is not None

    # stale_threshold_s=-1 makes the just-claimed (fresh-beat) job count as stale.
    reaped = reap_stale_once(queue, repository, datetime.now(UTC), stale_threshold_s=-1.0)
    assert reaped == [claimed.id]

    rows = _events(factory, "job.reclaimed")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["job_id"] == claimed.id
    assert payload["prior_claimed_by"] == "worker-dead"
    assert payload["reason"] == "stale_heartbeat"
    assert payload["run_id"] is None  # never transitioned → no run to fail


def test_no_emitter_means_no_rows(factory: sessionmaker[Session]) -> None:
    """Back-compat: with no emitter injected the seam is a silent no-op."""
    queue = JobQueue(factory)  # no emitter
    queue.enqueue_manual("sales", "dev")
    queue.claim_next("worker-1")
    queue.register_worker("w1", host="h", pid=1)
    queue.unregister_worker("w1")
    assert _events(factory) == []
