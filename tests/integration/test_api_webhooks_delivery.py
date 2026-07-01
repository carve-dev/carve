"""Webhooks (delivery): a subscriber receives a signed payload; HMAC verifies."""

from __future__ import annotations

import hashlib
import hmac

import httpx

from carve.api.webhooks import attempt_delivery
from carve.core.state.models import Event
from carve.core.state.store import StateStore
from tests.integration._api_support import make_state_store, subscriber_server


def _insert_event(store: StateStore, kind: str, payload: dict) -> None:
    with store.session_factory() as session:
        session.add(Event(kind=kind, payload=payload))
        session.commit()


async def test_webhook_delivers_signed_payload(postgres_state_store_url: str) -> None:
    store = make_state_store(postgres_state_store_url)
    received: list[dict] = []
    with subscriber_server(received, status_code=200) as base:
        webhook = store.webhooks.create(url=f"{base}/hook", event_filters=["run.failed"])
        _insert_event(store, "run.failed", {"run_id": "r1"})
        store.webhook_deliveries.enqueue_for_new_events()
        delivery = store.webhook_deliveries.pending_or_due_for_retry()[0]

        # allow_private_ips: the subscriber is on loopback (blocked by default).
        async with httpx.AsyncClient() as client:
            await attempt_delivery(client, store, delivery, allow_private_ips=True)

    assert len(received) == 1
    got = received[0]
    # The HMAC over the exact bytes sent verifies with the webhook secret.
    expected = "sha256=" + hmac.new(
        webhook.hmac_secret.encode("utf-8"), got["body"], hashlib.sha256
    ).hexdigest()
    assert got["headers"]["x-carve-signature"] == expected
    assert got["headers"]["x-carve-event"] == "run.failed"
    assert got["headers"]["x-carve-delivery-id"] == delivery.id

    row = store.webhook_deliveries.get(delivery.id)
    assert row is not None
    assert row.status == "delivered"
    assert row.response_status == 200


async def test_webhook_url_on_loopback_blocked_without_optin(
    postgres_state_store_url: str,
) -> None:
    store = make_state_store(postgres_state_store_url)
    received: list[dict] = []
    with subscriber_server(received, status_code=200) as base:
        store.webhooks.create(url=f"{base}/hook", event_filters=[])
        _insert_event(store, "run.failed", {"run_id": "r1"})
        store.webhook_deliveries.enqueue_for_new_events()
        delivery = store.webhook_deliveries.pending_or_due_for_retry()[0]
        async with httpx.AsyncClient() as client:
            await attempt_delivery(client, store, delivery)  # default: block private IPs

    # The SSRF guard refused the loopback POST — nothing was delivered.
    assert received == []
    row = store.webhook_deliveries.get(delivery.id)
    assert row is not None
    assert row.status == "abandoned"
