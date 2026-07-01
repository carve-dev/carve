"""``/api/v1/plans`` — plan listing/detail + creation (``carve plan``)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import (
    get_config,
    get_project_paths,
    get_state_store,
    pagination_params,
)
from carve.api.errors import ResourceNotFound
from carve.api.pagination import CANDIDATE_CEILING, PageParams, order_candidates, paginate
from carve.api.schemas import Page

if TYPE_CHECKING:
    from carve.core.config import Config
    from carve.core.config.paths import ProjectPaths
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


class PlanRequestIn(BaseModel):
    """Body for ``POST /plans`` — the goal to plan (optionally targeting a pipeline)."""

    goal: str
    pipeline_name: str | None = None


class PlanCreatedOut(PlanOut):
    """A freshly generated plan + its cost/impact estimate."""

    cost_usd: float
    tokens_input: int
    tokens_output: int


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


@router.post("", response_model=PlanCreatedOut, status_code=201)
def plan_create(
    body: PlanRequestIn,
    state_store: StateStore = Depends(get_state_store),
    config: Config = Depends(get_config),
    paths: ProjectPaths = Depends(get_project_paths),
) -> PlanCreatedOut:
    """Generate a plan for ``goal`` (``carve plan``), synchronously.

    A **sync** handler (like every read handler) so Starlette offloads it to the
    anyio threadpool — ``generate_plan`` runs an agent loop (``max_turns=30``) and
    can take minutes. **Threadpool-occupancy constraint:** each in-flight plan
    holds one worker thread for the agent-run's duration; concurrency is bounded
    by the threadpool (fine for single-user OSS; a hosted throughput concern would
    be the spec-first generic agent-run job queue, deliberately not built here).
    Because these sync agent-run handlers share the one bounded AnyIO threadpool
    with **every** other sync handler, a burst of concurrent plan/builds can starve
    ordinary read handlers; a bounded-concurrency limiter is deferred hosted work.
    (``/healthz`` is ``async`` so liveness stays off this threadpool.)
    Not domain-idempotent — every call mints a plan and spends tokens; an
    ``Idempotency-Key`` is the only client-retry dedup. **Known bounded gap:**
    ``IdempotencyMiddleware`` caches on *completion*, so a retry that arrives
    mid-flight (before the first plan finishes) finds no cache entry and can fire a
    second agent run — inherent to cache-on-completion over long sync ops; a true
    mid-flight reservation is out of scope here.
    """
    # Local import: keep the heavy orchestrator/agent stack off the module-import
    # path (imported per request, like serve.py's create_app import).
    from carve.cli.orchestrator.planner import generate_plan

    artifact = generate_plan(
        body.goal,
        config,
        paths.root,
        repository=state_store.repository,
        pipeline_name=body.pipeline_name,
        observer=None,
    )
    plan_row = state_store.repository.get_plan(artifact.id)
    if plan_row is None:  # pragma: no cover - generate_plan persists the row
        raise ResourceNotFound(f"Plan {artifact.id!r} was not persisted.")
    return PlanCreatedOut(
        **PlanOut.model_validate(plan_row).model_dump(),
        cost_usd=artifact.cost_usd,
        tokens_input=artifact.tokens_input,
        tokens_output=artifact.tokens_output,
    )


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
