"""``/api/v1/workers`` — read-only view of the worker pool (``carve worker`` state)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import get_state_store
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
    from carve.core.state.store import StateStore

router = APIRouter(prefix="/workers", tags=["workers"])


class WorkerOut(BaseModel):
    """A worker row on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    host: str
    pid: int
    label: str | None
    status: str
    started_at: datetime
    last_heartbeat_at: datetime


@router.get("", response_model=list[WorkerOut])
def list_workers(
    state_store: StateStore = Depends(get_state_store),
) -> list[WorkerOut]:
    """List registered workers, most-recently-started first."""
    return [WorkerOut.model_validate(w) for w in state_store.jobs.list_workers()]


@router.get("/{worker_id}", response_model=WorkerOut)
def get_worker(
    worker_id: str,
    state_store: StateStore = Depends(get_state_store),
) -> WorkerOut:
    """Fetch one worker by id."""
    worker = state_store.jobs.get_worker(worker_id)
    if worker is None:
        raise ResourceNotFound(f"Worker {worker_id!r} not found.")
    return WorkerOut.model_validate(worker)


__all__ = ["router"]
