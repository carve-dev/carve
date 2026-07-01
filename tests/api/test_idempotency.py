"""Idempotency middleware: same key+body → cached; diff body → 409; diff key → fresh."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from carve.api.auth import AuthMiddleware
from carve.api.errors import install_error_handlers
from carve.api.idempotency import IdempotencyMiddleware, _is_secret_returning_write
from tests.api.conftest import GOOD_TOKEN


def test_secret_returning_write_denylist_matches_exact_routes() -> None:
    assert _is_secret_returning_write("POST", "/api/v1/tokens") is True
    assert _is_secret_returning_write("POST", "/api/v1/webhooks") is True
    assert _is_secret_returning_write("POST", "/api/v1/webhooks/wh_123/rotate-secret") is True
    # Non-secret writes and reads are NOT denylisted.
    assert _is_secret_returning_write("GET", "/api/v1/tokens") is False
    assert _is_secret_returning_write("DELETE", "/api/v1/tokens/tok_1") is False
    assert _is_secret_returning_write("PATCH", "/api/v1/webhooks/wh_1") is False
    # No substring over-match: a nested/extra segment must not trip it.
    assert _is_secret_returning_write("POST", "/api/v1/webhooks/wh_1/a/rotate-secret") is False
    assert _is_secret_returning_write("POST", "/api/v1/tokens/extra") is False


@pytest.fixture
def counting_client(fake_store: MagicMock) -> tuple[TestClient, dict[str, int]]:
    calls = {"n": 0}
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(IdempotencyMiddleware, state_store=fake_store)
    app.add_middleware(AuthMiddleware, state_store=fake_store)

    @app.post("/api/v1/echo")
    async def echo(request: Request) -> dict[str, object]:
        calls["n"] += 1
        payload = await request.json()
        return {"call_count": calls["n"], "echo": payload}

    return TestClient(app), calls


_AUTH = {"Authorization": f"Bearer {GOOD_TOKEN}"}


def test_same_key_same_body_replays_cached(
    counting_client: tuple[TestClient, dict[str, int]],
) -> None:
    client, calls = counting_client
    headers = {**_AUTH, "Idempotency-Key": "k1"}
    first = client.post("/api/v1/echo", json={"a": 1}, headers=headers)
    second = client.post("/api/v1/echo", json={"a": 1}, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    # The handler ran once; the second response is the cached replay.
    assert calls["n"] == 1
    assert second.json() == first.json()
    assert second.headers.get("Idempotency-Replayed") == "true"


def test_same_key_different_body_is_409(
    counting_client: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = counting_client
    headers = {**_AUTH, "Idempotency-Key": "k2"}
    client.post("/api/v1/echo", json={"a": 1}, headers=headers)
    conflict = client.post("/api/v1/echo", json={"a": 2}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")
    assert conflict.json()["type"] == "https://carve.dev/errors/idempotency-key-reused"


def test_different_key_executes_fresh(
    counting_client: tuple[TestClient, dict[str, int]],
) -> None:
    client, calls = counting_client
    client.post("/api/v1/echo", json={"a": 1}, headers={**_AUTH, "Idempotency-Key": "k3"})
    client.post("/api/v1/echo", json={"a": 1}, headers={**_AUTH, "Idempotency-Key": "k4"})
    assert calls["n"] == 2


def test_no_key_always_executes(
    counting_client: tuple[TestClient, dict[str, int]],
) -> None:
    client, calls = counting_client
    client.post("/api/v1/echo", json={"a": 1}, headers=_AUTH)
    client.post("/api/v1/echo", json={"a": 1}, headers=_AUTH)
    assert calls["n"] == 2
