"""``/api/v1/agents`` — discovered agent definitions (``carve agents``).

Read-only over :class:`~carve.core.agents.discovery.AgentDiscovery` (builtin +
project ``carve/agents/``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from carve.api.dependencies import get_config, get_project_paths
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
    from carve.core.agents.loader import AgentFile
    from carve.core.config import Config
    from carve.core.config.paths import ProjectPaths

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentOut(BaseModel):
    name: str
    description: str
    model: str | None
    source: str
    tools: list[str]
    classifications: list[str]


class AgentDetail(AgentOut):
    body: str


def _discover(paths: ProjectPaths, config: Config) -> list[AgentFile]:
    from carve.core.agents.discovery import BUILTIN_AGENTS_DIR, AgentDiscovery

    agents_dir = paths.root / config.paths.agents_dir
    return AgentDiscovery.for_project(
        agents_dir=agents_dir, builtin_dir=BUILTIN_AGENTS_DIR
    ).discover()


def _summary(agent: AgentFile) -> AgentOut:
    return AgentOut(
        name=agent.name,
        description=agent.description,
        model=agent.model,
        source=str(agent.source_path),
        tools=list(agent.tools),
        classifications=list(agent.classifications),
    )


@router.get("", response_model=list[AgentOut])
def list_agents(
    paths: ProjectPaths = Depends(get_project_paths),
    config: Config = Depends(get_config),
) -> list[AgentOut]:
    """List discovered agents (builtin + project)."""
    return [_summary(a) for a in _discover(paths, config)]


@router.get("/{name}", response_model=AgentDetail)
def get_agent(
    name: str,
    paths: ProjectPaths = Depends(get_project_paths),
    config: Config = Depends(get_config),
) -> AgentDetail:
    """Fetch one agent definition (including its system-prompt body)."""
    for agent in _discover(paths, config):
        if agent.name == name:
            summary = _summary(agent)
            return AgentDetail(**summary.model_dump(), body=agent.body)
    raise ResourceNotFound(f"Agent {name!r} not found.")


__all__ = ["router"]
