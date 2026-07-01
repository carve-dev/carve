"""Full-coverage parity: every non-streaming REST endpoint has an MCP tool.

Driven by the LIVE ``app.openapi()`` (rendered offline over a ``MagicMock`` state
store — no Postgres), so a new REST endpoint that ships without an MCP tool fails
CI here. This extends rest-api's CLI↔REST parity test to the MCP surface.

Asserted against the real schema, NOT the spec's aspirational tool table (which
lists deploy/refine/ask endpoints that correctly do not exist in REST yet).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from carve.api.main import create_app
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig
from carve.mcp.tool_generator import generate_tools_from_openapi

#: The single endpoint MCP cannot adapt (synchronous tool_use can't stream).
_STREAMING_PATH_SUFFIX = "/stream"


def _live_openapi() -> dict[str, Any]:
    config = Config(
        project=ProjectConfig(name="mcp-coverage"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
    )
    return create_app(MagicMock(), config).openapi()


def _non_streaming_operations(schema: dict[str, Any]) -> set[tuple[str, str]]:
    ops: set[tuple[str, str]] = set()
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            if method.lower() == "get" and path.endswith(_STREAMING_PATH_SUFFIX):
                continue
            ops.add((method.upper(), path))
    return ops


def test_every_non_streaming_endpoint_has_a_tool() -> None:
    schema = _live_openapi()
    expected_ops = _non_streaming_operations(schema)
    tools = generate_tools_from_openapi(schema)

    # One tool per non-streaming operation, and names are unique.
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), f"duplicate tool names: {sorted(names)}"
    assert len(tools) == len(expected_ops), (
        f"tool count {len(tools)} != non-streaming op count {len(expected_ops)}; "
        "a REST endpoint may lack an MCP tool (or vice-versa)."
    )


def test_streaming_endpoint_generates_no_tool() -> None:
    schema = _live_openapi()
    tools = generate_tools_from_openapi(schema)
    assert not any("stream" in t.name for t in tools)
    # Sanity: the streaming endpoint really is present in the live schema.
    assert any(
        path.endswith(_STREAMING_PATH_SUFFIX) for path in schema["paths"]
    ), "expected a streaming endpoint in the live REST schema"


def test_documented_lifecycle_tools_are_generated() -> None:
    """plan/build/run write surface (PR #68) auto-generates its tools."""
    schema = _live_openapi()
    names = {t.name for t in generate_tools_from_openapi(schema)}
    assert {"plan_create", "build_run", "run_pipeline", "run_resume", "plans_list"} <= names


def test_deploy_tool_absent_until_rest_ships_it() -> None:
    """Correct-by-construction: no POST /deploys yet → no deploy tool (Increment 6)."""
    schema = _live_openapi()
    names = {t.name for t in generate_tools_from_openapi(schema)}
    assert "deploy_pipeline" not in names
    assert "deploys_list" in names  # the read side does exist
