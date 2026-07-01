"""``/api/v1/mcp-servers`` — configured MCP servers (``carve mcp-servers``).

Read-only over :func:`~carve.core.mcp.config.load_mcp_config`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from carve.api.dependencies import get_config, get_project_paths
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
    from carve.core.config import Config
    from carve.core.config.paths import ProjectPaths

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


class McpToolOut(BaseModel):
    name: str
    effects: list[str]


class McpServerOut(BaseModel):
    name: str
    command: str | None
    url: str | None
    tools: list[McpToolOut]


def _load(paths: ProjectPaths, config: Config):  # type: ignore[no-untyped-def]
    from carve.core.mcp.config import load_mcp_config

    return load_mcp_config(paths.root / config.paths.mcp_file)


def _to_out(server) -> McpServerOut:  # type: ignore[no-untyped-def]
    return McpServerOut(
        name=server.name,
        command=server.command,
        url=server.url,
        tools=[McpToolOut(name=t.name, effects=list(t.effects)) for t in server.tools],
    )


@router.get("", response_model=list[McpServerOut])
def list_mcp_servers(
    paths: ProjectPaths = Depends(get_project_paths),
    config: Config = Depends(get_config),
) -> list[McpServerOut]:
    """List configured MCP servers."""
    return [_to_out(s) for s in _load(paths, config).server]


@router.get("/{name}", response_model=McpServerOut)
def get_mcp_server(
    name: str,
    paths: ProjectPaths = Depends(get_project_paths),
    config: Config = Depends(get_config),
) -> McpServerOut:
    """Fetch one MCP server by name."""
    server = _load(paths, config).by_name(name)
    if server is None:
        raise ResourceNotFound(f"MCP server {name!r} not found.")
    return _to_out(server)


__all__ = ["router"]
