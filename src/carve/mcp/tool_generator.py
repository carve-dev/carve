"""Generate MCP tool definitions from Carve's REST OpenAPI schema.

This is the *only* Carve-specific derivation code on the list side: it walks the
live ``/api/openapi.json`` document, skips the streaming endpoint, derives a
stable ``<resource>_<verb>`` tool name per operation, and merges path/query/body
parameters into a single JSON-Schema ``inputSchema``. Every REST endpoint added
in the future gets a free MCP tool with zero per-endpoint code.

:func:`iter_operations` is the shared index both this module (list side) and the
:class:`~carve.mcp.adapter.RESTAdapter` (call side) build from, so the tool list
and the dispatch table can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp.types import Tool

#: REST routes live under this prefix; stripped before name derivation. Routes
#: outside it (the ``/healthz`` / ``/readyz`` probes) are named via overrides.
_API_PREFIX = "/api/v1"

#: HTTP method → verb suffix for collection-level (no path-param) writes.
_COLLECTION_VERB = {"post": "create", "put": "update", "patch": "update", "delete": "delete"}

#: HTTP method → verb suffix for item-level (trailing ``{id}``) operations.
_ITEM_VERB = {
    "get": "show",
    "post": "create",
    "put": "update",
    "patch": "update",
    "delete": "delete",
}

#: Hand-overrides for operations whose derived name would be awkward or wrong.
#: Keyed by ``(method, path_template)``. ``ask`` / ``asks`` land when the ask
#: capability mounts its router; harmless until then (no such path exists yet).
_NAME_OVERRIDES: dict[tuple[str, str], str] = {
    ("post", "/api/v1/builds"): "build_run",
    ("post", "/api/v1/runs"): "run_pipeline",
    ("post", "/api/v1/asks"): "ask",
    ("post", "/api/v1/memory/decisions"): "memory_append_decision",
    ("get", "/api/v1/builds/latest/{pipeline_name}/{target}"): "build_latest",
    ("get", "/healthz"): "healthz",
    ("get", "/readyz"): "readyz",
}


@dataclass(frozen=True)
class Operation:
    """One REST operation, resolved into everything the MCP layer needs.

    Shared by the tool generator (uses ``name``/``description``/``input_schema``)
    and the adapter (uses ``method``/``path``/``*_params`` to build the request).
    """

    name: str
    method: str
    path: str
    path_params: tuple[str, ...]
    query_params: tuple[str, ...]
    body_params: tuple[str, ...]
    input_schema: dict[str, Any]
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


def _is_param(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _is_streaming_endpoint(path: str, method: str) -> bool:
    """The one endpoint MCP can't adapt: ``GET /api/v1/runs/{run_id}/stream``.

    Streaming is inherently asynchronous; MCP ``tools/call`` is request/response.
    Clients that want the live stream connect to the REST SSE/WebSocket directly.
    """
    return method.lower() == "get" and path.endswith("/stream")


def _singularize(word: str) -> str:
    """Naive singular for a resource (``plans`` -> ``plan``, ``mcp-servers`` -> ``mcp_server``)."""
    w = word.replace("-", "_")
    if w.endswith("ss"):
        return w
    if w.endswith("s"):
        return w[:-1]
    return w


def _sanitize(name: str) -> str:
    return name.replace("-", "_")


def _derive_tool_name(path: str, method: str) -> str:
    """Map ``(path, method)`` → an MCP tool name via convention + overrides.

    Convention: ``POST /collection`` → ``{singular}_create``; ``GET /collection``
    → ``{collection}_list``; ``GET /collection/{id}`` → ``{singular}_show``;
    ``POST /collection/{id}/{action}`` → ``{singular}_{action}``. Overrides in
    :data:`_NAME_OVERRIDES` win.
    """
    method = method.lower()
    override = _NAME_OVERRIDES.get((method, path))
    if override is not None:
        return override

    trimmed = path[len(_API_PREFIX) :] if path.startswith(_API_PREFIX) else path
    segments = [s for s in trimmed.split("/") if s]
    if not segments:
        return _sanitize(f"root_{method}")

    literals = [s for s in segments if not _is_param(s)]
    collection = literals[0] if literals else segments[0].strip("{}")
    singular = _singularize(collection)
    has_params = any(_is_param(s) for s in segments)

    if not has_params:
        if len(literals) == 1:
            if method == "get":
                name = f"{collection}_list"
            else:
                name = f"{singular}_{_COLLECTION_VERB.get(method, method)}"
        else:
            # /collection/sub-literal (e.g. metrics/costs) — join by resource.
            sub = literals[-1]
            prefix = collection if method == "get" else singular
            name = f"{prefix}_{sub}"
    elif _is_param(segments[-1]):
        # Item-level: /collection/{id}
        name = f"{singular}_{_ITEM_VERB.get(method, method)}"
    else:
        # Action / sub-resource on an item: /collection/{id}/action
        name = f"{singular}_{segments[-1]}"

    return _sanitize(name)


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``#/...`` JSON reference against the OpenAPI document."""
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node if isinstance(node, dict) else {}


def _resolve_body(
    body_schema: dict[str, Any], root: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Resolve a ``requestBody`` schema → ``(properties, required)``.

    Handles a direct ``$ref`` (required body) and an ``anyOf: [$ref, null]``
    wrapper (optional body — nothing becomes required). Carve's request bodies
    are flat objects, so a single ``$ref`` hop is sufficient.
    """
    if "$ref" in body_schema:
        obj = _resolve_ref(body_schema["$ref"], root)
        required = obj.get("required")
        return obj.get("properties", {}), list(required) if required else []
    if "anyOf" in body_schema:
        for branch in body_schema["anyOf"]:
            if isinstance(branch, dict) and "$ref" in branch:
                obj = _resolve_ref(branch["$ref"], root)
                return obj.get("properties", {}), []  # optional body → no required fields
    if body_schema.get("type") == "object":
        required = body_schema.get("required")
        return body_schema.get("properties", {}), list(required) if required else []
    return {}, []


def _derive_operation(
    path: str, method: str, operation: dict[str, Any], root: dict[str, Any]
) -> Operation:
    """Build an :class:`Operation` (name + param locations + merged inputSchema)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    query_params: list[str] = []

    for param in operation.get("parameters", []):
        if not isinstance(param, dict):
            continue
        pname = param["name"]
        location = param.get("in")
        pschema = dict(param.get("schema", {}))
        if "description" in param and "description" not in pschema:
            pschema["description"] = param["description"]
        properties[pname] = pschema
        if location == "path":
            path_params.append(pname)
            required.append(pname)
        elif location == "query":
            query_params.append(pname)
            if param.get("required"):
                required.append(pname)

    body_params: list[str] = []
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        json_schema = (
            request_body.get("content", {}).get("application/json", {}).get("schema", {})
        )
        body_props, body_required = _resolve_body(json_schema, root)
        for bname, bschema in body_props.items():
            properties[bname] = bschema
            body_params.append(bname)
        required.extend(r for r in body_required if r not in required)

    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required

    return Operation(
        name=_derive_tool_name(path, method),
        method=method.upper(),
        path=path,
        path_params=tuple(path_params),
        query_params=tuple(query_params),
        body_params=tuple(body_params),
        input_schema=input_schema,
        description=operation.get("description") or operation.get("summary") or "",
        tags=tuple(operation.get("tags", [])),
    )


def iter_operations(openapi_schema: dict[str, Any]) -> list[Operation]:
    """Resolve every non-streaming REST operation into an :class:`Operation`.

    The single source of truth both the tool list and the adapter's routing
    table derive from — so a new REST endpoint appears in both by construction.
    """
    operations: list[Operation] = []
    for path, methods in openapi_schema.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            if _is_streaming_endpoint(path, method):
                continue
            operations.append(_derive_operation(path, method, operation, openapi_schema))
    return operations


def generate_tools_from_openapi(openapi_schema: dict[str, Any]) -> list[Tool]:
    """Turn the REST OpenAPI schema into a list of MCP :class:`~mcp.types.Tool`."""
    return [
        Tool(name=op.name, description=op.description or None, inputSchema=op.input_schema)
        for op in iter_operations(openapi_schema)
    ]


def build_routing_table(openapi_schema: dict[str, Any]) -> dict[str, Operation]:
    """Map ``tool_name → Operation`` for the adapter's dispatch."""
    return {op.name: op for op in iter_operations(openapi_schema)}


__all__ = [
    "Operation",
    "build_routing_table",
    "generate_tools_from_openapi",
    "iter_operations",
]
