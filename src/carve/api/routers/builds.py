"""``/api/v1/builds`` — build detail (``carve build`` artifacts).

Read surface over ``Repository.get_build`` / ``latest_build_for``. The
``ConfigDriftError`` a build raises maps to a 409 problem+json via the shared
exception handler (the spec's drift example round-trips through it).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import get_config, get_project_paths, get_state_store
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
    from carve.core.config import Config
    from carve.core.config.paths import ProjectPaths
    from carve.core.state.store import StateStore

router = APIRouter(prefix="/builds", tags=["builds"])


class BuildOut(BaseModel):
    """A build row on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    pipeline_name: str
    plan_id: str
    target: str
    created_at: datetime
    manifest_json: dict[str, Any]
    commit_sha: str | None
    pr_url: str | None
    deployed_at: datetime | None


class BuildRequestIn(BaseModel):
    """Body for ``POST /builds`` — the plan to build.

    ``force`` is **phase-only**: it re-runs the agent against an already-built
    plan; it NEVER bypasses the config-drift gate (drift is checked first in
    ``build_plan``). No destination-path override is exposed over HTTP.
    """

    plan_id: str
    force: bool = False


class BuildResultOut(BaseModel):
    """The result of a build run."""

    build_id: str | None
    run_id: str
    pipeline_name: str
    target: str
    files_written: list[str]
    cost_usd: float
    success: bool


@router.post("", response_model=BuildResultOut)
def build_run(
    body: BuildRequestIn,
    state_store: StateStore = Depends(get_state_store),
    config: Config = Depends(get_config),
    paths: ProjectPaths = Depends(get_project_paths),
) -> BuildResultOut:
    """Build the pipeline described by ``plan_id`` (``carve build``).

    Runs synchronously and can take minutes (it drives an agent loop), returning
    the build result. Returns 409 if the plan has drifted from current config
    (``force`` cannot bypass drift), 404 if the plan is unknown. Idempotent for an
    already-built plan against unchanged config (returns the existing build without
    a new agent run); pass an ``Idempotency-Key`` header to dedupe client retries.
    """
    # Implementation notes (kept out of the OpenAPI/MCP-visible docstring above):
    #  * A *sync* handler so Starlette offloads it to the anyio threadpool —
    #    ``build_plan`` runs an agent loop. Each in-flight build holds one worker
    #    thread for the run's duration, sharing the one bounded AnyIO threadpool
    #    with all other sync handlers; a burst of concurrent plan/builds can starve
    #    ordinary read handlers (a bounded-concurrency limiter is deferred hosted
    #    work; ``/healthz`` is ``async`` so liveness stays off this pool).
    #  * Idempotency gap: ``IdempotencyMiddleware`` caches on completion, so a
    #    client retry mid-build (before the first commits) can fire a second run.
    # Plan-not-found → 404 pre-check (build_plan raises BuildError → 400 for both
    # missing-plan and wrong-phase; narrow the missing-plan case here).
    if state_store.repository.get_plan(body.plan_id) is None:
        raise ResourceNotFound(f"Plan {body.plan_id!r} not found.")

    from carve.cli.orchestrator.builder import build_plan

    artifact = build_plan(
        body.plan_id,
        config,
        paths.root,
        repository=state_store.repository,
        force=body.force,
        observer=None,
    )
    return BuildResultOut(
        build_id=artifact.build_id,
        run_id=artifact.run_id,
        pipeline_name=artifact.pipeline_name,
        target=artifact.target,
        files_written=list(artifact.files_written),
        cost_usd=artifact.cost_usd,
        success=artifact.success,
    )


@router.get("/{build_id}", response_model=BuildOut)
def get_build(
    build_id: str,
    state_store: StateStore = Depends(get_state_store),
) -> BuildOut:
    """Fetch one build by id."""
    build = state_store.repository.get_build(build_id)
    if build is None:
        raise ResourceNotFound(f"Build {build_id!r} not found.")
    return BuildOut.model_validate(build)


@router.get("/latest/{pipeline_name}/{target}", response_model=BuildOut)
def latest_build(
    pipeline_name: str,
    target: str,
    state_store: StateStore = Depends(get_state_store),
) -> BuildOut:
    """The most recent build for ``(pipeline_name, target)``."""
    build = state_store.repository.latest_build_for(pipeline_name, target)
    if build is None:
        raise ResourceNotFound(f"No build for {pipeline_name!r}/{target!r}.")
    return BuildOut.model_validate(build)


__all__ = ["router"]
