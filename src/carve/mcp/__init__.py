"""Carve's MCP server — a thin, auto-generated adapter over the REST API.

This package is Carve *being* an MCP server (exposing every non-streaming REST
endpoint as an MCP tool). It is the semantic inverse of :mod:`carve.core.mcp`,
which is Carve *consuming* other MCP servers — the two never import each other.

Only two modules carry Carve-specific logic: :mod:`~carve.mcp.tool_generator`
(OpenAPI → tool definitions) and :mod:`~carve.mcp.adapter` (tool call → REST
request). Everything else is the official ``mcp`` SDK.
"""

from __future__ import annotations

from mcp.types import Tool as MCPTool

from carve.mcp.adapter import MCPToolError, RESTAdapter
from carve.mcp.auth import MCPAuthError, resolve_token
from carve.mcp.server import build_server, fetch_openapi_schema, run_http, run_stdio
from carve.mcp.tool_generator import generate_tools_from_openapi

__all__ = [
    "MCPAuthError",
    "MCPTool",
    "MCPToolError",
    "RESTAdapter",
    "build_server",
    "fetch_openapi_schema",
    "generate_tools_from_openapi",
    "resolve_token",
    "run_http",
    "run_stdio",
]
