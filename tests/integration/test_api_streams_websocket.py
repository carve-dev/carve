"""Streams (WebSocket): backfill + event sequence; auth at connection upgrade."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from carve.api.main import create_app
from carve.core.state.models import Event
from carve.core.state.store import StateStore
from tests.integration._api_support import make_config, make_state_store, mint_token


def _insert_event(store: StateStore, kind: str, payload: dict) -> None:
    with store.session_factory() as session:
        session.add(Event(kind=kind, payload=payload))
        session.commit()


def _client(store: StateStore) -> TestClient:
    return TestClient(create_app(store, make_config("x")))


def test_websocket_backfills_and_closes_on_terminal(postgres_state_store_url: str) -> None:
    store = make_state_store(postgres_state_store_url)
    token = mint_token(store)
    run_id = store.repository.create_run(kind="pipeline", target_id="p")
    _insert_event(store, "run.started", {"run_id": run_id, "pipeline": "p"})
    _insert_event(store, "step.completed", {"run_id": run_id, "step_id": "s1"})
    _insert_event(store, "run.succeeded", {"run_id": run_id})

    client = _client(store)
    headers = {"Authorization": f"Bearer {token}"}
    frames: list[dict] = []
    with client.websocket_connect(f"/api/v1/runs/{run_id}/stream", headers=headers) as ws:
        with pytest.raises(WebSocketDisconnect):
            while True:
                frames.append(ws.receive_json())

    kinds = [f["event"] for f in frames]
    assert kinds == ["run.started", "step.completed", "run.succeeded"]
    assert frames[0]["data"]["run_id"] == run_id


def test_websocket_rejects_missing_token(postgres_state_store_url: str) -> None:
    store = make_state_store(postgres_state_store_url)
    run_id = store.repository.create_run(kind="pipeline", target_id="p")
    client = _client(store)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/v1/runs/{run_id}/stream"):
            pass


def test_websocket_rejects_unknown_run(postgres_state_store_url: str) -> None:
    store = make_state_store(postgres_state_store_url)
    token = mint_token(store)
    client = _client(store)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/runs/does-not-exist/stream",
            headers={"Authorization": f"Bearer {token}"},
        ):
            pass
