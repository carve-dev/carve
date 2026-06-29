"""The durable EventEmitter writes one ``events`` row per emit, best-effort.

Postgres-fixture-gated (the emitter writes to the real ``events`` table). Covers:
``emit`` persists a row with the right ``kind``/``payload``/``tenant_id``; a JSONB
payload round-trips; a fresh row is unprocessed (``processed_at IS NULL``, on the
partial index); and a failing emit is swallowed (the best-effort stance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Event
from carve.runtime.events import EventEmitter

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def session_factory(postgres_state_store_url: str) -> sessionmaker[Session]:
    config = Config(
        project=ProjectConfig(name="events-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return create_session_factory(engine)


def _events(factory: sessionmaker[Session]) -> list[Event]:
    with factory() as session:
        return list(session.scalars(sa.select(Event).order_by(Event.id.asc())).all())


def test_emit_writes_one_durable_row(session_factory: sessionmaker[Session]) -> None:
    emitter = EventEmitter(session_factory)
    emitter.emit("job.queued", {"job_id": "job_1", "pipeline": "sales"})

    rows = _events(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "job.queued"
    assert row.payload == {"job_id": "job_1", "pipeline": "sales"}
    assert row.tenant_id == 1
    assert row.occurred_at is not None


def test_emit_respects_explicit_tenant_id(session_factory: sessionmaker[Session]) -> None:
    emitter = EventEmitter(session_factory)
    emitter.emit("run.started", {"run_id": "r1"}, tenant_id=7)
    rows = _events(session_factory)
    assert rows[0].tenant_id == 7


def test_jsonb_payload_round_trips(session_factory: sessionmaker[Session]) -> None:
    """A nested payload (lists, nulls, nested dicts) round-trips through JSONB."""
    emitter = EventEmitter(session_factory)
    payload = {
        "run_id": "r1",
        "duration_ms": 42,
        "error_message": None,
        "outputs": {"tables": ["charges", "refunds"]},
    }
    emitter.emit("step.completed", payload)
    assert _events(session_factory)[0].payload == payload


def test_fresh_row_is_unprocessed(session_factory: sessionmaker[Session]) -> None:
    """A freshly-emitted row has ``processed_at IS NULL`` — it rides the partial
    ``ix_events_unprocessed`` index a future relay/webhook scans."""
    emitter = EventEmitter(session_factory)
    emitter.emit("run.succeeded", {"run_id": "r1"})

    rows = _events(session_factory)
    assert rows[0].processed_at is None
    # The unprocessed scan (the partial-index query) finds it.
    with session_factory() as session:
        unprocessed = session.scalars(sa.select(Event).where(Event.processed_at.is_(None))).all()
    assert len(list(unprocessed)) == 1


def test_emit_against_a_down_session_is_swallowed() -> None:
    """A failing emit is logged + swallowed — events never kill the work.

    No Postgres needed: a factory that raises on call exercises the best-effort
    guard directly (the heartbeat's logged-and-swallowed stance).
    """

    def _broken_factory() -> object:
        raise RuntimeError("db down")

    emitter = EventEmitter(_broken_factory)  # type: ignore[arg-type]
    # Must NOT raise — the run/loop that emitted it carries on.
    emitter.emit("run.failed", {"run_id": "r1", "error_message": "boom"})
