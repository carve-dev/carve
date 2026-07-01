"""``/api/v1/runs`` — run listing/detail/logs + the live event stream.

Wraps ``Repository.list_runs``/``get_run``/``get_logs`` (``carve runs`` /
``carve logs``). The stream is served two ways on ``/runs/{run_id}/stream``: an
SSE ``GET`` here, and a WebSocket route registered in ``main.py`` pointing at
:data:`stream_handler`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import get_state_store, pagination_params
from carve.api.errors import ResourceNotFound
from carve.api.pagination import CANDIDATE_CEILING, PageParams, order_candidates, paginate
from carve.api.schemas import Page
from carve.api.streams import run_stream_sse, run_stream_websocket

if TYPE_CHECKING:
    from carve.core.state.store import StateStore

router = APIRouter(prefix="/runs", tags=["runs"])

#: WebSocket handler for ``/api/v1/runs/{run_id}/stream`` (registered in main.py).
stream_handler = run_stream_websocket


class RunOut(BaseModel):
    """A run row on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    target_id: str
    target: str | None
    pipeline_name: str | None
    parent_run_id: str | None
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error_message: str | None
    tokens_input: int
    tokens_output: int
    cost_usd: float
    created_at: datetime


class LogOut(BaseModel):
    """A log line on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: str
    timestamp: datetime
    level: str
    source: str
    message: str


@router.get("", response_model=Page[RunOut])
def list_runs(
    status: str | None = None,
    pipeline_name: str | None = None,
    params: PageParams = Depends(pagination_params),
    state_store: StateStore = Depends(get_state_store),
) -> Page[RunOut]:
    """List runs newest-first (cursor-paginated), optionally filtered."""
    candidates = state_store.repository.list_runs(
        status=status, pipeline_name=pipeline_name, limit=CANDIDATE_CEILING
    )
    ordered = order_candidates(candidates, id_of=lambda r: r.id, created_of=lambda r: r.created_at)
    result = paginate(ordered, params, id_of=lambda r: r.id, created_of=lambda r: r.created_at)
    return Page[RunOut].build(result, [RunOut.model_validate(r) for r in result.items])


@router.get("/{run_id}", response_model=RunOut)
def get_run(
    run_id: str,
    state_store: StateStore = Depends(get_state_store),
) -> RunOut:
    """Fetch one run by id."""
    run = state_store.repository.get_run(run_id)
    if run is None:
        raise ResourceNotFound(f"Run {run_id!r} not found.")
    return RunOut.model_validate(run)


@router.get("/{run_id}/logs", response_model=list[LogOut])
def get_run_logs(
    run_id: str,
    since_id: int | None = None,
    state_store: StateStore = Depends(get_state_store),
) -> list[LogOut]:
    """Return a run's logs in insertion order (optionally after ``since_id``)."""
    run = state_store.repository.get_run(run_id)
    if run is None:
        raise ResourceNotFound(f"Run {run_id!r} not found.")
    logs = state_store.repository.get_logs(run_id, since_id=since_id)
    return [LogOut.model_validate(log) for log in logs]


@router.get("/{run_id}/stream")
async def stream_run(run_id: str, request: Request):  # type: ignore[no-untyped-def]
    """SSE event stream for a run (``Accept: text/event-stream``).

    The WebSocket transport for the same path is registered in ``main.py``.
    """
    return await run_stream_sse(request)


__all__ = ["router", "stream_handler"]
