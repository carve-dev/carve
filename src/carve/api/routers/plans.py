"""``/api/v1/plans`` — plan listing/detail (``carve plan`` design artifacts)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import get_state_store, pagination_params
from carve.api.errors import ResourceNotFound
from carve.api.pagination import CANDIDATE_CEILING, PageParams, order_candidates, paginate
from carve.api.schemas import Page

if TYPE_CHECKING:
    from carve.core.state.store import StateStore

router = APIRouter(prefix="/plans", tags=["plans"])


class PlanOut(BaseModel):
    """A plan row on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    parent_plan_id: str | None
    goal: str
    config_hash: str
    carve_version: str
    task_graph_json: dict[str, Any]
    file_path: str
    phase: str
    pipeline_name: str | None
    created_at: datetime
    expires_at: datetime


@router.get("", response_model=Page[PlanOut])
def list_plans(
    pipeline_name: str | None = None,
    params: PageParams = Depends(pagination_params),
    state_store: StateStore = Depends(get_state_store),
) -> Page[PlanOut]:
    """List plans newest-first (cursor-paginated)."""
    candidates = state_store.repository.list_plans(
        pipeline_name=pipeline_name, limit=CANDIDATE_CEILING
    )
    ordered = order_candidates(candidates, id_of=lambda p: p.id, created_of=lambda p: p.created_at)
    result = paginate(ordered, params, id_of=lambda p: p.id, created_of=lambda p: p.created_at)
    return Page[PlanOut].build(result, [PlanOut.model_validate(p) for p in result.items])


@router.get("/{plan_id}", response_model=PlanOut)
def get_plan(
    plan_id: str,
    state_store: StateStore = Depends(get_state_store),
) -> PlanOut:
    """Fetch one plan by id."""
    plan = state_store.repository.get_plan(plan_id)
    if plan is None:
        raise ResourceNotFound(f"Plan {plan_id!r} not found.")
    return PlanOut.model_validate(plan)


__all__ = ["router"]
