"""``/api/v1/jobs`` — read-only view of the durable work queue (``JobQueue``).

Thin: it wraps the ``jobs`` table directly (there is no dedicated ``carve jobs``
CLI). Enqueue/claim/transition stay internal to the runtime.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import get_state_store, pagination_params
from carve.api.errors import ResourceNotFound
from carve.api.pagination import CANDIDATE_CEILING, PageParams, order_candidates, paginate
from carve.api.schemas import Page

if TYPE_CHECKING:
    from carve.core.state.store import StateStore

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    """A job row on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    pipeline: str
    target: str
    status: str
    trigger: str
    required_label: str | None
    scheduled_for: datetime | None
    run_id: str | None
    claimed_by: str | None
    claimed_at: datetime | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    created_at: datetime


@router.get("", response_model=Page[JobOut])
def list_jobs(
    status: str | None = None,
    params: PageParams = Depends(pagination_params),
    state_store: StateStore = Depends(get_state_store),
) -> Page[JobOut]:
    """List jobs newest-first (cursor-paginated)."""
    candidates = state_store.jobs.list_jobs(status=status, limit=CANDIDATE_CEILING)
    ordered = order_candidates(candidates, id_of=lambda j: j.id, created_of=lambda j: j.created_at)
    result = paginate(ordered, params, id_of=lambda j: j.id, created_of=lambda j: j.created_at)
    return Page[JobOut].build(result, [JobOut.model_validate(j) for j in result.items])


@router.get("/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    state_store: StateStore = Depends(get_state_store),
) -> JobOut:
    """Fetch one job by id."""
    job = state_store.jobs.get_job(job_id)
    if job is None:
        raise ResourceNotFound(f"Job {job_id!r} not found.")
    return JobOut.model_validate(job)


__all__ = ["router"]
