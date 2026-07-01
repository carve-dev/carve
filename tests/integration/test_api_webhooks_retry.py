"""Webhooks (retry): a 503 subscriber → retries on schedule → eventually abandoned."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from carve.api.webhooks import RETRY_SCHEDULE_S, attempt_delivery
from carve.core.state.models import Event
from carve.core.state.store import StateStore
from tests.integration._api_support import make_state_store, subscriber_server


def _insert_event(store: StateStore, kind: str, payload: dict) -> None:
    with store.session_factory() as session:
        session.add(Event(kind=kind, payload=payload))
        session.commit()


def test_retry_schedule_is_the_documented_sequence() -> None:
    # The literal schedule: 30s, 1m, 5m, 15m, 1h, 3h — six delays, abandon after.
    assert RETRY_SCHEDULE_S == (30, 60, 300, 900, 3600, 10800)


async def test_retries_follow_schedule_then_abandoned(postgres_state_store_url: str) -> None:
    store = make_state_store(postgres_state_store_url)
    deliveries = store.webhook_deliveries
    received: list[dict] = []

    with subscriber_server(received, status_code=503) as base:
        store.webhooks.create(url=f"{base}/hook", event_filters=[])
        _insert_event(store, "run.failed", {"run_id": "r1"})
        deliveries.enqueue_for_new_events()
        delivery_id = deliveries.pending_or_due_for_retry()[0].id

        async with httpx.AsyncClient() as client:
            # Each failed attempt schedules the next per RETRY_SCHEDULE_S (6 delays).
            for expected_count in range(1, len(RETRY_SCHEDULE_S) + 1):
                delivery = deliveries.get(delivery_id)
                assert delivery is not None
                before = datetime.now(UTC)
                await attempt_delivery(client, store, delivery, allow_private_ips=True)
                row = deliveries.get(delivery_id)
                assert row is not None
                assert row.status == "pending"
                assert row.retry_count == expected_count
                assert row.next_retry_at is not None
                # The scheduled delay matches the documented magnitude for this attempt.
                expected_delay = RETRY_SCHEDULE_S[expected_count - 1]
                actual_delay = (row.next_retry_at - before).total_seconds()
                assert abs(actual_delay - expected_delay) < 5.0, (
                    f"attempt {expected_count}: expected ~{expected_delay}s, got {actual_delay}s"
                )

            # The schedule is now exhausted: the next attempt abandons.
            delivery = deliveries.get(delivery_id)
            assert delivery is not None
            await attempt_delivery(client, store, delivery, allow_private_ips=True)
            final = deliveries.get(delivery_id)
            assert final is not None
            assert final.status == "abandoned"

    # Every attempt reached the subscriber (6 retries scheduled + the final).
    assert len(received) == len(RETRY_SCHEDULE_S) + 1
    # No longer due once abandoned.
    assert deliveries.pending_or_due_for_retry() == []
