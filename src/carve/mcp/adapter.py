"""The REST adapter — the ONLY code in this package that talks to REST.

Pure translation, no business logic: a ``tools/call`` becomes an HTTP request
(method + path substitution + query + JSON body split by where each argument
belongs), and an RFC 9457 ``problem+json`` error becomes a structured
:class:`MCPToolError`. Every scrap of Carve behavior lives behind REST; if you
are tempted to add logic here, it belongs in a REST router instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from carve.mcp.tool_generator import build_routing_table

if TYPE_CHECKING:
    from carve.mcp.tool_generator import Operation


class MCPToolError(Exception):
    """A REST error surfaced as a structured MCP tool error.

    ``code`` is the problem ``type`` slug, ``message`` the human-facing detail,
    and ``data`` the full problem+json payload (attached as ``structuredContent``).
    """

    def __init__(self, code: str, message: str, data: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class RESTAdapter:
    """Translate MCP tool calls into Carve REST requests over one async client."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        openapi_schema: dict[str, Any],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # The bearer secret is set once, here, on the client's default headers —
        # never logged, echoed, or stored anywhere else. ``transport`` is a test
        # seam (e.g. ``httpx.MockTransport``); production leaves it ``None``.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"authorization": f"Bearer {token}"},
            transport=transport,
        )
        self.routing_table: dict[str, Operation] = build_routing_table(openapi_schema)

    async def aclose(self) -> None:
        await self._client.aclose()

    def format_path(self, operation: Operation, args: dict[str, Any]) -> str:
        """Substitute ``{param}`` placeholders in the path from ``args``."""
        path = operation.path
        for name in operation.path_params:
            if name not in args:
                raise MCPToolError(
                    "invalid-arguments",
                    f"Missing required path parameter {name!r} for tool {operation.name!r}.",
                    {"tool": operation.name, "missing": name},
                )
            # Percent-encode each segment value (``safe=""`` also escapes ``/``) so
            # a value can't inject extra path segments or a query/fragment.
            path = path.replace(f"{{{name}}}", quote(str(args[name]), safe=""))
        return path

    def extract_query(self, operation: Operation, args: dict[str, Any]) -> dict[str, Any]:
        """Pull the query-string arguments (skip ``None`` — an unset optional)."""
        return {
            name: args[name]
            for name in operation.query_params
            if name in args and args[name] is not None
        }

    def extract_body(self, operation: Operation, args: dict[str, Any]) -> dict[str, Any] | None:
        """Pull the JSON-body arguments, or ``None`` when the operation takes none."""
        if not operation.body_params:
            return None
        return {name: args[name] for name in operation.body_params if name in args}

    async def call(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Dispatch one tool call to REST and return the parsed JSON response.

        Raises :class:`MCPToolError` for any ``>= 400`` response (converted from
        problem+json) and for unknown tools.
        """
        operation = self.routing_table.get(tool_name)
        if operation is None:
            raise MCPToolError(
                "unknown-tool",
                f"No such tool: {tool_name!r}.",
                {"tool": tool_name},
            )

        path = self.format_path(operation, args)
        query = self.extract_query(operation, args)
        body = self.extract_body(operation, args)

        try:
            response = await self._client.request(
                operation.method, path, params=query or None, json=body
            )
        except httpx.RequestError as exc:
            # Transport-level failure (connection refused, DNS, timeout — e.g.
            # `carve serve` not running). Surface it as a structured tool error
            # the model can act on, not an unhandled exception.
            raise MCPToolError(
                "transport-error",
                f"Could not reach the Carve REST API ({type(exc).__name__}). "
                "Is `carve serve` running?",
                {"tool": tool_name, "error": type(exc).__name__},
            ) from exc
        if response.status_code >= 400:
            raise self._convert_error(response)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    def _convert_error(self, response: httpx.Response) -> MCPToolError:
        """Convert a ``problem+json`` (RFC 9457) error response into an MCP tool error."""
        status = response.status_code
        try:
            problem = response.json()
        except ValueError:
            problem = None
        if isinstance(problem, dict):
            code = str(problem.get("type", f"http-{status}"))
            message = str(
                problem.get("detail") or problem.get("title") or f"REST error (HTTP {status})"
            )
            return MCPToolError(code=code, message=message, data=problem)
        return MCPToolError(
            code=f"http-{status}",
            message=f"REST error (HTTP {status}).",
            data={"status": status, "body": response.text},
        )


__all__ = ["MCPToolError", "RESTAdapter"]
