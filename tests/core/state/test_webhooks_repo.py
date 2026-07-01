"""Webhooks + WebhookDeliveries repos: CRUD, rotate, fan-out, retry ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Event
from carve.core.state.webhooks import WebhookDeliveries, Webhooks


@pytest.fixture
def factory(postgres_state_store_url: str):  # type: ignore[no-untyped-def]
    config = Config(
        project=ProjectConfig(name="wh-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return create_session_factory(engine)


def _insert_event(factory, kind: str, payload: dict) -> int:  # type: ignore[no-untyped-def]
    with factory() as session:
        event = Event(kind=kind, payload=payload)
        session.add(event)
        session.commit()
        session.refresh(event)
        return event.id


def test_create_returns_secret_and_get_round_trips(factory) -> None:  # type: ignore[no-untyped-def]
    repo = Webhooks(factory)
    webhook = repo.create(url="https://example.test/hook", event_filters=["run.failed"])
    assert webhook.hmac_secret
    fetched = repo.get(webhook.id)
    assert fetched is not None
    assert fetched.url == "https://example.test/hook"
    assert fetched.event_filters == ["run.failed"]
    assert fetched.active is True


def test_update_and_rotate_secret(factory) -> None:  # type: ignore[no-untyped-def]
    repo = Webhooks(factory)
    webhook = repo.create(url="https://example.test/a", event_filters=[])
    old_secret = webhook.hmac_secret

    updated = repo.update(webhook.id, url="https://example.test/b", active=False)
    assert updated is not None
    assert updated.url == "https://example.test/b"
    assert updated.active is False

    rotated = repo.rotate_secret(webhook.id)
    assert rotated is not None
    assert rotated.hmac_secret != old_secret


def test_delete_removes_webhook(factory) -> None:  # type: ignore[no-untyped-def]
    repo = Webhooks(factory)
    webhook = repo.create(url="https://example.test/x", event_filters=[])
    assert repo.delete(webhook.id) is True
    assert repo.get(webhook.id) is None
    assert repo.delete(webhook.id) is False


def test_fan_out_matches_filters_and_marks_events_processed(factory) -> None:  # type: ignore[no-untyped-def]
    webhooks = Webhooks(factory)
    deliveries = WebhookDeliveries(factory)
    webhooks.create(url="https://example.test/fail", event_filters=["run.failed"])

    matching = _insert_event(factory, "run.failed", {"run_id": "r1"})
    _insert_event(factory, "run.succeeded", {"run_id": "r2"})

    enqueued = deliveries.enqueue_for_new_events()
    assert enqueued == 1  # only the run.failed event matched

    due = deliveries.pending_or_due_for_retry()
    assert len(due) == 1
    assert due[0].event_id == matching

    # A second pass enqueues nothing (events are now processed).
    assert deliveries.enqueue_for_new_events() == 0


def test_empty_filters_match_all_events(factory) -> None:  # type: ignore[no-untyped-def]
    webhooks = Webhooks(factory)
    deliveries = WebhookDeliveries(factory)
    webhooks.create(url="https://example.test/all", event_filters=[])
    _insert_event(factory, "run.failed", {"run_id": "r1"})
    _insert_event(factory, "step.started", {"run_id": "r1"})
    assert deliveries.enqueue_for_new_events() == 2


def test_mark_retry_then_due_and_delivered(factory) -> None:  # type: ignore[no-untyped-def]
    webhooks = Webhooks(factory)
    deliveries = WebhookDeliveries(factory)
    webhooks.create(url="https://example.test/r", event_filters=[])
    _insert_event(factory, "run.failed", {"run_id": "r1"})
    deliveries.enqueue_for_new_events()
    delivery = deliveries.pending_or_due_for_retry()[0]

    # Schedule a retry in the future — not yet due.
    future = datetime.now(UTC) + timedelta(hours=1)
    deliveries.mark_retry(delivery.id, retry_count=1, next_retry_at=future, response_status=503)
    assert deliveries.pending_or_due_for_retry(datetime.now(UTC)) == []
    # Due once its next_retry_at passes.
    assert len(deliveries.pending_or_due_for_retry(future + timedelta(seconds=1))) == 1

    deliveries.mark_delivered(delivery.id, response_status=200)
    row = deliveries.get(delivery.id)
    assert row is not None
    assert row.status == "delivered"
    assert row.response_status == 200


def test_mark_abandoned(factory) -> None:  # type: ignore[no-untyped-def]
    webhooks = Webhooks(factory)
    deliveries = WebhookDeliveries(factory)
    webhooks.create(url="https://example.test/a", event_filters=[])
    _insert_event(factory, "run.failed", {"run_id": "r1"})
    deliveries.enqueue_for_new_events()
    delivery = deliveries.pending_or_due_for_retry()[0]
    deliveries.mark_abandoned(delivery.id, response_status=503)
    row = deliveries.get(delivery.id)
    assert row is not None
    assert row.status == "abandoned"
    assert deliveries.pending_or_due_for_retry() == []
