"""Unit tests: the adapter builds the right HTTP request from a tool call."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from carve.mcp.adapter import MCPToolError, RESTAdapter


def _schema_with_path_query_body() -> dict[str, Any]:
    """A synthetic op exercising path + query + body params at once."""
    return {
        "openapi": "3.1.0",
        "paths": {
            "/api/v1/widgets/{widget_id}": {
                "put": {
                    "summary": "Update a widget",
                    "parameters": [
                        {"name": "widget_id", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                        {"name": "dry_run", "in": "query", "required": False,
                         "schema": {"type": "boolean"}},
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/WidgetIn"}
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "WidgetIn": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}, "color": {"type": "string"}},
                }
            }
        },
    }


def _capturing_adapter(
    schema: dict[str, Any], *, status: int = 200, json_body: Any = None
) -> tuple[RESTAdapter, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json=json_body if json_body is not None else {"ok": True})

    adapter = RESTAdapter(
        base_url="http://rest.local",
        token="secret-token",
        openapi_schema=schema,
        transport=httpx.MockTransport(handler),
    )
    return adapter, captured


async def test_path_query_body_split_into_the_right_request() -> None:
    adapter, captured = _capturing_adapter(_schema_with_path_query_body())
    result = await adapter.call(
        "widget_update",
        {"widget_id": "w-42", "dry_run": True, "name": "sprocket", "color": "blue"},
    )
    await adapter.aclose()

    assert result == {"ok": True}
    (request,) = captured
    assert request.method == "PUT"
    assert request.url.path == "/api/v1/widgets/w-42"  # path param substituted
    assert dict(request.url.params) == {"dry_run": "true"}  # query param only
    import json

    assert json.loads(request.content) == {"name": "sprocket", "color": "blue"}  # body only


async def test_bearer_token_is_set_once_on_the_header() -> None:
    adapter, captured = _capturing_adapter(_schema_with_path_query_body())
    await adapter.call("widget_update", {"widget_id": "w-1", "name": "x"})
    await adapter.aclose()
    (request,) = captured
    assert request.headers["authorization"] == "Bearer secret-token"


async def test_path_param_is_percent_encoded() -> None:
    adapter, captured = _capturing_adapter(_schema_with_path_query_body())
    await adapter.call("widget_update", {"widget_id": "a/b c", "name": "x"})
    await adapter.aclose()
    (request,) = captured
    # ``/`` and space are escaped so the value can't inject extra path segments.
    assert request.url.raw_path == b"/api/v1/widgets/a%2Fb%20c"


async def test_transport_error_becomes_structured_tool_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = RESTAdapter(
        base_url="http://rest.local",
        token="t",
        openapi_schema=_schema_with_path_query_body(),
        transport=httpx.MockTransport(boom),
    )
    with pytest.raises(MCPToolError) as excinfo:
        await adapter.call("widget_update", {"widget_id": "w-1", "name": "x"})
    await adapter.aclose()
    assert excinfo.value.code == "transport-error"
    assert "carve serve" in excinfo.value.message


async def test_optional_query_param_omitted_when_absent() -> None:
    adapter, captured = _capturing_adapter(_schema_with_path_query_body())
    await adapter.call("widget_update", {"widget_id": "w-1", "name": "x"})
    await adapter.aclose()
    (request,) = captured
    assert "dry_run" not in dict(request.url.params)


async def test_missing_path_param_raises_tool_error() -> None:
    adapter, _ = _capturing_adapter(_schema_with_path_query_body())
    with pytest.raises(MCPToolError) as excinfo:
        await adapter.call("widget_update", {"name": "x"})
    await adapter.aclose()
    assert "widget_id" in excinfo.value.message


async def test_unknown_tool_raises_tool_error() -> None:
    adapter, _ = _capturing_adapter(_schema_with_path_query_body())
    with pytest.raises(MCPToolError) as excinfo:
        await adapter.call("does_not_exist", {})
    await adapter.aclose()
    assert excinfo.value.code == "unknown-tool"


async def test_empty_response_body_returns_empty_dict() -> None:
    adapter, _ = _capturing_adapter(_schema_with_path_query_body(), status=204)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    adapter = RESTAdapter(
        base_url="http://rest.local",
        token="t",
        openapi_schema=_schema_with_path_query_body(),
        transport=httpx.MockTransport(handler),
    )
    result = await adapter.call("widget_update", {"widget_id": "w-1", "name": "x"})
    await adapter.aclose()
    assert result == {}
