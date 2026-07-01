"""``/api/v1/schedules`` — schedule listing + live mutation (``carve schedule``).

Wraps the :class:`~carve.core.state.schedules.Schedules` repo. Mutations record
``source="api"`` and the caller's ``actor_token_id`` in the audit trail.
``ScheduleNotFound`` maps to 404 via the shared exception handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import get_identity, get_state_store

if TYPE_CHECKING:
    from carve.core.state.store import StateStore
    from carve.core.state.tokens import Identity

router = APIRouter(prefix="/schedules", tags=["schedules"])


class ScheduleOut(BaseModel):
    """A schedule row on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    pipeline: str
    cron: str
    target: str
    paused: bool
    paused_by: str | None
    pause_reason: str | None
    timezone: str
    last_fired_at: datetime | None
    next_fires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ScheduleChangeOut(BaseModel):
    """A schedule audit row on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    pipeline: str
    change_kind: str
    source: str
    reason: str | None
    actor_token_id: str | None
    changed_at: datetime


class PauseBody(BaseModel):
    reason: str | None = None


class SetCronBody(BaseModel):
    cron: str
    target: str | None = None
    timezone: str | None = None
    reason: str | None = None


@router.get("", response_model=list[ScheduleOut])
def list_schedules(
    state_store: StateStore = Depends(get_state_store),
) -> list[ScheduleOut]:
    """List all schedules."""
    return [ScheduleOut.model_validate(s) for s in state_store.schedules.list_all()]


@router.get("/{pipeline}", response_model=ScheduleOut)
def get_schedule(
    pipeline: str,
    state_store: StateStore = Depends(get_state_store),
) -> ScheduleOut:
    """Fetch a schedule by pipeline."""
    from carve.api.errors import ResourceNotFound

    schedule = state_store.schedules.get(pipeline)
    if schedule is None:
        raise ResourceNotFound(f"No schedule for pipeline {pipeline!r}.")
    return ScheduleOut.model_validate(schedule)


@router.post("/{pipeline}/pause", response_model=ScheduleOut)
def pause_schedule(
    pipeline: str,
    body: PauseBody | None = None,
    state_store: StateStore = Depends(get_state_store),
    identity: Identity = Depends(get_identity),
) -> ScheduleOut:
    """Pause a schedule."""
    reason = body.reason if body else None
    schedule = state_store.schedules.pause(
        pipeline, reason=reason, source="api", actor_token_id=identity.token_id
    )
    return ScheduleOut.model_validate(schedule)


@router.post("/{pipeline}/resume", response_model=ScheduleOut)
def resume_schedule(
    pipeline: str,
    body: PauseBody | None = None,
    state_store: StateStore = Depends(get_state_store),
    identity: Identity = Depends(get_identity),
) -> ScheduleOut:
    """Resume a paused schedule."""
    reason = body.reason if body else None
    schedule = state_store.schedules.resume(
        pipeline, reason=reason, source="api", actor_token_id=identity.token_id
    )
    return ScheduleOut.model_validate(schedule)


@router.put("/{pipeline}", response_model=ScheduleOut)
def set_cron(
    pipeline: str,
    body: SetCronBody,
    state_store: StateStore = Depends(get_state_store),
    identity: Identity = Depends(get_identity),
) -> ScheduleOut:
    """Set (upsert) a schedule's cron (+ optional target/timezone)."""
    schedule = state_store.schedules.set_cron(
        pipeline,
        body.cron,
        target=body.target,
        timezone=body.timezone,
        reason=body.reason,
        source="api",
        actor_token_id=identity.token_id,
    )
    return ScheduleOut.model_validate(schedule)


@router.get("/{pipeline}/changes", response_model=list[ScheduleChangeOut])
def list_changes(
    pipeline: str,
    state_store: StateStore = Depends(get_state_store),
) -> list[ScheduleChangeOut]:
    """A pipeline's schedule audit trail, newest first."""
    return [
        ScheduleChangeOut.model_validate(c) for c in state_store.schedules.list_changes(pipeline)
    ]


__all__ = ["router"]
