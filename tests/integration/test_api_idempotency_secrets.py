"""Secret-returning writes are never cached in ``idempotency_keys`` (SSRF/at-rest).

``POST /api/v1/tokens`` and ``POST /api/v1/webhooks`` return plaintext secrets;
the idempotency middleware must short-circuit them so a live bearer token /
``hmac_secret`` never lands in the cache, and a stale secret is never replayed.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from starlette.testclient import TestClient

from carve.api.main import create_app
from carve.core.state.models import IdempotencyKey
from carve.core.state.store import StateStore
from tests.integration._api_support import make_config, make_state_store


def _all_cached_bodies(store: StateStore) -> str:
    with store.session_factory() as session:
        rows = session.scalars(select(IdempotencyKey)).all()
        return json.dumps([r.response_body for r in rows])


def _auth_client(store: StateStore) -> tuple[TestClient, dict[str, str]]:
    _tid, token = store.tokens.create(scopes=["*"])
    client = TestClient(create_app(store, make_config("x")))
    return client, {"Authorization": f"Bearer {token}"}


def test_post_tokens_is_not_cached_or_replayed(postgres_state_store_url: str) -> None:
    store = make_state_store(postgres_state_store_url)
    client, auth = _auth_client(store)
    headers = {**auth, "Idempotency-Key": "same-key"}

    first = client.post("/api/v1/tokens", json={"scopes": ["*"]}, headers=headers)
    second = client.post("/api/v1/tokens", json={"scopes": ["*"]}, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201

    tok1 = first.json()["token"]
    tok2 = second.json()["token"]
    # NOT a stale replay: each call minted a fresh, distinct token.
    assert tok1 != tok2
    assert second.headers.get("Idempotency-Replayed") is None

    # And neither plaintext token was persisted in the idempotency cache.
    cached = _all_cached_bodies(store)
    assert tok1 not in cached
    assert tok2 not in cached


def test_post_webhooks_secret_is_not_cached(postgres_state_store_url: str) -> None:
    store = make_state_store(postgres_state_store_url)
    client, auth = _auth_client(store)
    headers = {**auth, "Idempotency-Key": "wh-key"}

    first = client.post(
        "/api/v1/webhooks",
        json={"url": "https://example.test/h", "event_filters": []},
        headers=headers,
    )
    second = client.post(
        "/api/v1/webhooks",
        json={"url": "https://example.test/h", "event_filters": []},
        headers=headers,
    )
    assert first.status_code == 201
    assert second.status_code == 201
    secret1 = first.json()["hmac_secret"]
    secret2 = second.json()["hmac_secret"]
    assert secret1 != secret2  # fresh execution, not a cached replay

    cached = _all_cached_bodies(store)
    assert secret1 not in cached
    assert secret2 not in cached
