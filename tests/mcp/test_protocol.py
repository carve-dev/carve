"""Unit test: initialize / tools/list / tools/call round-trip via the SDK session.

Uses the SDK's in-memory client↔server transport, so ``initialize`` (protocol
negotiation), ``tools/list``, and ``tools/call`` are exercised against the real
``ServerSession`` — conformance for free, no hand-rolled JSON-RPC parser.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from mcp.shared.memory import create_connected_server_and_client_session

from carve.mcp.adapter import MCPToolError
from carve.mcp.server import build_server
from tests.mcp.conftest import sample_openapi

_UNSET = object()


class _FakeAdapter:
    """A stand-in for RESTAdapter that records calls and returns canned JSON.

    ``result`` is returned verbatim (including ``None`` / lists / scalars) so the
    ``CallToolResult`` wrapping in ``build_server`` can be exercised faithfully.
    """

    def __init__(self, result: Any = _UNSET, *, error: MCPToolError | None = None) -> None:
        self.result = {"items": [], "next_cursor": None} if result is _UNSET else result
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool_name: str, args: dict[str, Any]) -> Any:
        self.calls.append((tool_name, args))
        if self.error is not None:
            raise self.error
        return self.result


async def test_initialize_and_list_tools_round_trip() -> None:
    adapter = _FakeAdapter()
    server = build_server(adapter, sample_openapi())  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()

    names = {tool.name for tool in result.tools}
    assert "plans_list" in names
    assert "run_pipeline" in names
    # Streaming endpoint never surfaces as a tool.
    assert not any("stream" in n for n in names)


async def test_tools_call_dispatches_to_adapter_and_returns_structured_content() -> None:
    adapter = _FakeAdapter(result={"items": [{"id": "plan_1"}], "next_cursor": None})
    server = build_server(adapter, sample_openapi())  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("plans_list", {})

    assert result.isError is False
    assert adapter.calls == [("plans_list", {})]
    assert result.structuredContent == {"items": [{"id": "plan_1"}], "next_cursor": None}
    assert isinstance(result.content[0], mcp_types.TextContent)


async def test_tools_call_top_level_array_response_is_wrapped_as_text() -> None:
    """~13 live REST endpoints return a top-level JSON array; it must not break.

    The SDK's auto-wrap mishandles a bare list, so ``build_server`` wraps every
    payload itself: the array goes into a ``TextContent`` and ``structuredContent``
    stays unset (structured content must be an object).
    """
    import json

    payload = [{"id": "run_1"}, {"id": "run_2"}]
    adapter = _FakeAdapter(result=payload)
    server = build_server(adapter, sample_openapi())  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("run_logs", {"run_id": "run_1"})

    assert result.isError is False
    assert result.structuredContent is None  # arrays are never structuredContent
    assert isinstance(result.content[0], mcp_types.TextContent)
    assert json.loads(result.content[0].text) == payload


async def test_tools_call_scalar_response_is_wrapped_as_text() -> None:
    adapter = _FakeAdapter(result=42)
    server = build_server(adapter, sample_openapi())  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("plans_list", {})

    assert result.isError is False
    assert result.structuredContent is None
    assert isinstance(result.content[0], mcp_types.TextContent)
    assert result.content[0].text == "42"


async def test_tools_call_null_response_is_wrapped_as_text() -> None:
    adapter = _FakeAdapter(result=None)
    server = build_server(adapter, sample_openapi())  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("plans_list", {})

    assert result.isError is False
    assert result.structuredContent is None
    assert isinstance(result.content[0], mcp_types.TextContent)
    assert result.content[0].text == "null"


async def test_tools_call_error_surfaces_as_mcp_tool_error() -> None:
    error = MCPToolError(
        code="https://carve.dev/errors/not-found",
        message="Plan not found.",
        data={"status": 404, "detail": "Plan not found."},
    )
    adapter = _FakeAdapter(error=error)
    server = build_server(adapter, sample_openapi())  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("plan_show", {"plan_id": "nope"})

    assert result.isError is True
    assert isinstance(result.content[0], mcp_types.TextContent)
    assert result.content[0].text == "Plan not found."
    assert result.structuredContent == {"status": 404, "detail": "Plan not found."}
