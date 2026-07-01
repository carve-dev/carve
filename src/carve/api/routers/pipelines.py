"""``/api/v1/pipelines`` — pipeline listing/detail/lineage (``carve pipelines``)."""

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

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


class PipelineOut(BaseModel):
    """A pipeline row on the wire."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    pipeline_dir: str
    current_build_id: str | None
    created_at: datetime
    updated_at: datetime
    last_run_id: str | None
    last_run_status: str | None
    last_run_at: datetime | None


class LineageOut(BaseModel):
    """A pipeline's plan lineage + recent runs."""

    pipeline: PipelineOut
    current_plan_id: str | None
    parent_plan_ids: list[str]
    child_plan_ids: list[str]
    recent_run_ids: list[str]


@router.get("", response_model=Page[PipelineOut])
def list_pipelines(
    params: PageParams = Depends(pagination_params),
    state_store: StateStore = Depends(get_state_store),
) -> Page[PipelineOut]:
    """List pipelines (cursor-paginated)."""
    candidates = state_store.repository.list_pipelines(limit=CANDIDATE_CEILING)
    ordered = order_candidates(
        candidates, id_of=lambda p: p.name, created_of=lambda p: p.created_at
    )
    result = paginate(ordered, params, id_of=lambda p: p.name, created_of=lambda p: p.created_at)
    return Page[PipelineOut].build(result, [PipelineOut.model_validate(p) for p in result.items])


@router.get("/{name}", response_model=PipelineOut)
def get_pipeline(
    name: str,
    state_store: StateStore = Depends(get_state_store),
) -> PipelineOut:
    """Fetch one pipeline by name."""
    pipeline = state_store.repository.get_pipeline(name)
    if pipeline is None:
        raise ResourceNotFound(f"Pipeline {name!r} not found.")
    return PipelineOut.model_validate(pipeline)


@router.get("/{name}/lineage", response_model=LineageOut)
def get_pipeline_lineage(
    name: str,
    state_store: StateStore = Depends(get_state_store),
) -> LineageOut:
    """Plan lineage (parent chain, children) + recent runs for a pipeline."""
    lineage = state_store.repository.get_pipeline_lineage(name)
    if lineage is None:
        raise ResourceNotFound(f"Pipeline {name!r} not found.")
    return LineageOut(
        pipeline=PipelineOut.model_validate(lineage.pipeline),
        current_plan_id=lineage.current_plan.id if lineage.current_plan else None,
        parent_plan_ids=[p.id for p in lineage.parent_chain],
        child_plan_ids=[p.id for p in lineage.children],
        recent_run_ids=[r.id for r in lineage.recent_runs],
    )


__all__ = ["router"]
