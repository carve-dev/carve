"""Streams (SSE): backfill over ``text/event-stream`` + the 30s keepalive heartbeat.

The keepalive cadence is validated against the shared streaming core with a
shrunk interval (the real default is 30s — too slow for a test).
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from carve.api import streams
from carve.api.main import create_app
from carve.core.state.models import Event
from carve.core.state.store import StateStore
from tests.integration._api_support import make_config, make_state_store, mint_token


def _insert_event(store: StateStore, kind: str, payload: dict) -> None:
    with store.session_factory() as session:
        session.add(Event(kind=kind, payload=payload))
        session.commit()


def test_sse_streams_backfill_frames(postgres_state_store_url: str) -> None:
    store = make_state_store(postgres_state_store_url)
    token = mint_token(store)
    run_id = store.repository.create_run(kind="pipeline", target_id="p")
    _insert_event(store, "run.started", {"run_id": run_id})
    _insert_event(store, "run.succeeded", {"run_id": run_id})

    client = TestClient(create_app(store, make_config("x")))
    with client.stream(
        "GET",
        f"/api/v1/runs/{run_id}/stream",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())

    frames = [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    kinds = [f["event"] for f in frames]
    assert kinds == ["run.started", "run.succeeded"]


def test_keepalive_default_is_30s() -> None:
    # The production heartbeat cadence is 30s (the tests below shrink it to keep
    # the run fast — this pins the real default the spec requires).
    assert streams.KEEPALIVE_INTERVAL_S == 30.0


async def test_stream_core_emits_keepalive_on_an_open_run(
    postgres_state_store_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no terminal event, the live tail heartbeats — deterministically (no race)."""
    assert streams.KEEPALIVE_INTERVAL_S == 30.0  # real default, before we shrink it
    monkeypatch.setattr(streams, "KEEPALIVE_INTERVAL_S", 0.01)
    monkeypatch.setattr(streams, "POLL_INTERVAL_S", 0.005)

    store = make_state_store(postgres_state_store_url)
    run_id = store.repository.create_run(kind="pipeline", target_id="p")
    _insert_event(store, "run.started", {"run_id": run_id})

    frames: list[dict] = []
    gen = streams.iter_run_events(store, run_id, started_at=None)
    try:
        # Consume the backfill, then wait for the first live heartbeat. No
        # terminal event is inserted, so there is no timing race with the close.
        async for frame in gen:
            frames.append(frame)
            if frame["event"] == "_keepalive":
                break
    finally:
        await gen.aclose()

    assert frames[0]["event"] == "run.started"
    assert frames[-1]["event"] == "_keepalive"


async def test_stream_core_closes_on_terminal_event(
    postgres_state_store_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal event in the backfill closes the stream — deterministic."""
    monkeypatch.setattr(streams, "POLL_INTERVAL_S", 0.005)
    store = make_state_store(postgres_state_store_url)
    run_id = store.repository.create_run(kind="pipeline", target_id="p")
    _insert_event(store, "run.started", {"run_id": run_id})
    _insert_event(store, "run.succeeded", {"run_id": run_id})

    frames = [
        frame
        async for frame in streams.iter_run_events(store, run_id, started_at=None)
    ]
    kinds = [f["event"] for f in frames]
    assert kinds == ["run.started", "run.succeeded"]
