"""Live run event streaming over WebSocket or SSE.

Both transports share one core (:func:`iter_run_events`): a **backfill** of the
run's events since ``started_at``, then a live tail that polls the ``events``
read-side, emitting a ``{"event": "_keepalive"}`` heartbeat every 30s and closing
when the run reaches a terminal state. The WebSocket path authenticates at the
connection upgrade (``BaseHTTPMiddleware`` never runs for websockets); the SSE
path is a normal ``GET`` under ``/api/v1`` and is authenticated by
``AuthMiddleware``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from carve.api.errors import ResourceNotFound
from carve.core.state.events_read import TERMINAL_RUN_EVENTS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request

    from carve.core.state.models import Event
    from carve.core.state.store import StateStore

logger = logging.getLogger(__name__)

#: Heartbeat cadence (module-level so tests can shrink it).
KEEPALIVE_INTERVAL_S = 30.0
#: How often the live tail polls the events table.
POLL_INTERVAL_S = 0.5
#: Hard ceiling so a never-terminating run can't hold a connection forever.
MAX_STREAM_SECONDS = 3600.0

_KEEPALIVE_FRAME = {"event": "_keepalive"}


def serialize_event(event: Event) -> dict[str, Any]:
    """Project an ``events`` row to the wire shape ``{event, timestamp, data}``."""
    return {
        "event": event.kind,
        "timestamp": event.occurred_at.isoformat() if event.occurred_at else None,
        "data": event.payload,
    }


async def iter_run_events(
    state_store: StateStore,
    run_id: str,
    *,
    started_at: Any = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield serialized events for ``run_id``: backfill, then live tail + keepalive.

    Terminates after a terminal ``run.*`` event or ``MAX_STREAM_SECONDS``.
    """
    reader = state_store.events
    backfill = await run_in_threadpool(reader.backfill, run_id, since=started_at)
    last_id = 0
    for event in backfill:
        last_id = max(last_id, event.id)
        yield serialize_event(event)
        if event.kind in TERMINAL_RUN_EVENTS:
            return

    last_keepalive = time.monotonic()
    deadline = time.monotonic() + MAX_STREAM_SECONDS
    while time.monotonic() < deadline:
        new_events = await run_in_threadpool(reader.tail_after, run_id, after_id=last_id)
        terminal = False
        for event in new_events:
            last_id = max(last_id, event.id)
            yield serialize_event(event)
            if event.kind in TERMINAL_RUN_EVENTS:
                terminal = True
        if terminal:
            return
        now = time.monotonic()
        if now - last_keepalive >= KEEPALIVE_INTERVAL_S:
            yield dict(_KEEPALIVE_FRAME)
            last_keepalive = now
        await asyncio.sleep(POLL_INTERVAL_S)


def _authenticate_ws(websocket: WebSocket, state_store: StateStore) -> Any:
    """Resolve the WS connection's bearer token (header or ``?token=``) to an Identity.

    The header path is primary. NOTE(rest-api): see issue — the ``?token=`` query
    fallback (for browsers that can't set WS headers) should become a single-use,
    short-lived stream ticket rather than the raw bearer.
    """
    header = websocket.headers.get("authorization", "")
    token: str | None = None
    if header.startswith("Bearer "):
        token = header[len("Bearer ") :].strip()
    else:
        token = websocket.query_params.get("token")
    if not token:
        return None
    return state_store.tokens.find_by_token(token)


async def run_stream_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint for ``/api/v1/runs/{run_id}/stream``.

    Authenticates at upgrade, backfills + tails, closes on the terminal event.
    """
    state_store: StateStore = websocket.app.state.state_store
    run_id = websocket.path_params["run_id"]

    identity = await run_in_threadpool(_authenticate_ws, websocket, state_store)
    if identity is None:
        await websocket.close(code=1008)  # policy violation (unauthenticated)
        return

    run = await run_in_threadpool(state_store.repository.get_run, run_id)
    if run is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        async for frame in iter_run_events(state_store, run_id, started_at=run.started_at):
            await websocket.send_json(frame)
    except WebSocketDisconnect:
        return
    except Exception:  # pragma: no cover - defensive; log and drop the stream
        logger.warning("run stream websocket error for run %s", run_id, exc_info=True)
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed


async def run_stream_sse(request: Request) -> StreamingResponse:
    """SSE endpoint for ``GET /api/v1/runs/{run_id}/stream`` (``Accept: text/event-stream``)."""
    state_store: StateStore = request.app.state.state_store
    run_id = request.path_params["run_id"]
    run = await run_in_threadpool(state_store.repository.get_run, run_id)
    if run is None:
        raise ResourceNotFound(f"Run {run_id!r} not found.")

    started_at = run.started_at

    async def _body() -> AsyncIterator[bytes]:
        async for frame in iter_run_events(state_store, run_id, started_at=started_at):
            yield f"data: {json.dumps(frame)}\n\n".encode()

    return StreamingResponse(
        _body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = [
    "KEEPALIVE_INTERVAL_S",
    "MAX_STREAM_SECONDS",
    "POLL_INTERVAL_S",
    "iter_run_events",
    "run_stream_sse",
    "run_stream_websocket",
    "serialize_event",
]
