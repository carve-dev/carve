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

from carve.api.dependencies import get_state_store
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
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
