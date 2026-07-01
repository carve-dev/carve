"""Unit tests: OpenAPI → MCP tool definitions (naming, exclusions, schema merge)."""

from __future__ import annotations

from typing import Any

from carve.mcp.tool_generator import (
    build_routing_table,
    generate_tools_from_openapi,
    iter_operations,
)


def _tools_by_name(fragment: dict[str, Any]) -> dict[str, Any]:
    return {tool.name: tool for tool in generate_tools_from_openapi(fragment)}


def test_naming_convention_matches_documented_table(openapi_fragment: dict[str, Any]) -> None:
    names = set(_tools_by_name(openapi_fragment))
    expected = {
        "plans_list",  # GET /collection
        "plan_create",  # POST /collection
        "plan_show",  # GET /collection/{id}
        "run_resume",  # POST /collection/{id}/{action}
        "run_logs",  # GET /collection/{id}/{subresource}
        "memory_show",  # GET /memory/{kind}
        # override map:
        "build_run",  # POST /builds
        "run_pipeline",  # POST /runs
        "memory_append_decision",  # POST /memory/decisions
        "build_latest",  # GET /builds/latest/{pipeline}/{target}
    }
    assert expected <= names


def test_streaming_endpoint_is_excluded(openapi_fragment: dict[str, Any]) -> None:
    names = set(_tools_by_name(openapi_fragment))
    assert not any("stream" in name for name in names)
    # ...but the sibling non-streaming logs endpoint on the same resource is kept.
    assert "run_logs" in names


def test_tool_names_are_unique(openapi_fragment: dict[str, Any]) -> None:
    tools = generate_tools_from_openapi(openapi_fragment)
    names = [t.name for t in tools]
    assert len(names) == len(set(names))


def test_path_params_are_required_strings(openapi_fragment: dict[str, Any]) -> None:
    tools = _tools_by_name(openapi_fragment)
    schema = tools["plan_show"].inputSchema
    assert schema["type"] == "object"
    assert "plan_id" in schema["properties"]
    assert schema["required"] == ["plan_id"]


def test_query_params_are_optional_and_typed(openapi_fragment: dict[str, Any]) -> None:
    tools = _tools_by_name(openapi_fragment)
    schema = tools["run_logs"].inputSchema
    # run_id is a required path param; since_id is an optional query param.
    assert "since_id" in schema["properties"]
    assert schema["properties"]["since_id"] == {"type": "integer"}
    assert schema["required"] == ["run_id"]
    assert "since_id" not in schema["required"]


def test_request_body_merges_into_input_schema(openapi_fragment: dict[str, Any]) -> None:
    tools = _tools_by_name(openapi_fragment)
    schema = tools["plan_create"].inputSchema
    assert set(schema["properties"]) == {"goal", "pipeline_name"}
    assert schema["required"] == ["goal"]


def test_optional_body_marks_nothing_required(openapi_fragment: dict[str, Any]) -> None:
    # DecisionIn is required-body here, but the overall required list only carries
    # its own required fields, merged after path/query params.
    tools = _tools_by_name(openapi_fragment)
    schema = tools["memory_append_decision"].inputSchema
    assert set(schema["properties"]) == {"title", "body", "reviewers"}
    assert set(schema["required"]) == {"title", "body"}


def test_description_prefers_description_then_summary(openapi_fragment: dict[str, Any]) -> None:
    tools = _tools_by_name(openapi_fragment)
    assert tools["plan_create"].description == "Create a plan"  # from `description`
    assert tools["plan_show"].description == "Show a plan"  # falls back to `summary`


def test_routing_table_covers_every_generated_tool(openapi_fragment: dict[str, Any]) -> None:
    routing = build_routing_table(openapi_fragment)
    tool_names = {t.name for t in generate_tools_from_openapi(openapi_fragment)}
    assert set(routing) == tool_names
    # Param locations are captured for dispatch.
    resume = routing["run_resume"]
    assert resume.method == "POST"
    assert resume.path == "/api/v1/runs/{run_id}/resume"
    assert resume.path_params == ("run_id",)


def test_iter_operations_skips_only_streaming(openapi_fragment: dict[str, Any]) -> None:
    ops = iter_operations(openapi_fragment)
    paths = {op.path for op in ops}
    assert "/api/v1/runs/{run_id}/stream" not in paths
    assert "/api/v1/runs/{run_id}/logs" in paths
