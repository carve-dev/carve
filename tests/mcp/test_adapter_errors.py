"""Unit tests: REST problem+json errors → structured MCP tool errors."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from carve.mcp.adapter import MCPToolError, RESTAdapter


def _schema() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "paths": {
            "/api/v1/plans/{plan_id}": {
                "get": {
                    "summary": "Show a plan",
                    "parameters": [
                        {"name": "plan_id", "in": "path", "required": True,
                         "schema": {"type": "string"}}
                    ],
                }
            }
        },
    }


def _adapter_returning(response: httpx.Response) -> RESTAdapter:
    return RESTAdapter(
        base_url="http://rest.local",
        token="t",
        openapi_schema=_schema(),
        transport=httpx.MockTransport(lambda _req: response),
    )


async def test_problem_json_404_converts_to_structured_tool_error() -> None:
    problem = {
        "type": "https://carve.dev/errors/not-found",
        "title": "Not Found",
        "status": 404,
        "detail": "Plan 'plan_x' not found.",
    }
    adapter = _adapter_returning(
        httpx.Response(404, json=problem, headers={"content-type": "application/problem+json"})
    )
    with pytest.raises(MCPToolError) as excinfo:
        await adapter.call("plan_show", {"plan_id": "plan_x"})
    await adapter.aclose()

    error = excinfo.value
    assert error.code == "https://carve.dev/errors/not-found"
    assert error.message == "Plan 'plan_x' not found."  # detail preferred
    assert error.data == problem  # full payload retained for structuredContent


async def test_problem_json_500_falls_back_to_title_when_no_detail() -> None:
    problem = {
        "type": "https://carve.dev/errors/internal",
        "title": "Internal Error",
        "status": 500,
    }
    adapter = _adapter_returning(httpx.Response(500, json=problem))
    with pytest.raises(MCPToolError) as excinfo:
        await adapter.call("plan_show", {"plan_id": "p"})
    await adapter.aclose()
    assert excinfo.value.message == "Internal Error"


async def test_non_json_error_body_is_handled() -> None:
    adapter = _adapter_returning(httpx.Response(502, text="upstream boom"))
    with pytest.raises(MCPToolError) as excinfo:
        await adapter.call("plan_show", {"plan_id": "p"})
    await adapter.aclose()
    error = excinfo.value
    assert error.code == "http-502"
    assert "502" in error.message
    assert error.data["status"] == 502
