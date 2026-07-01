"""Wire the auto-generated tools + REST adapter into an MCP low-level server.

Everything protocol-shaped (``initialize`` handshake, capability negotiation,
JSON-RPC framing, the session loop) comes from the official ``mcp`` SDK. Carve
contributes exactly two handlers: ``list_tools`` (returns the OpenAPI-generated
catalog) and ``call_tool`` (delegates to the :class:`RESTAdapter`). The two
transports are the SDK's own — ``stdio_server`` and the Streamable HTTP session
manager on a Starlette app — never hand-rolled.

Server capabilities declare **tools only** (no resources/prompts/sampling).
"""

from __future__ import annotations

import contextlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool

from carve.mcp.adapter import MCPToolError
from carve.mcp.tool_generator import generate_tools_from_openapi
from carve.version import __version__

if TYPE_CHECKING:
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.types import ASGIApp, Receive, Scope, Send

    from carve.mcp.adapter import RESTAdapter

#: The path a Streamable HTTP MCP client connects to (SDK convention).
HTTP_MOUNT_PATH = "/mcp"


def fetch_openapi_schema(server_url: str, token: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Fetch ``<server_url>/api/openapi.json`` once at startup.

    The schema changes only between Carve releases, so a single fetch per process
    is the right granularity (equivalent to per-session for the stdio subprocess).
    The bearer header is sent defensively; the token value is never logged.
    """
    response = httpx.get(
        f"{server_url.rstrip('/')}/api/openapi.json",
        headers={"authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        schema = response.json()
    except ValueError as exc:
        # A wrong --server-url can return HTML/text (e.g. a proxy error page);
        # surface a clean error instead of a raw JSONDecodeError traceback.
        raise ValueError(
            f"{server_url} did not return a JSON OpenAPI document "
            "(is --server-url pointing at the Carve REST API?)."
        ) from exc
    if not isinstance(schema, dict):
        raise ValueError("OpenAPI document was not a JSON object.")
    return schema


def build_server(adapter: RESTAdapter, openapi_schema: dict[str, Any]) -> Server[Any, Any]:
    """Build the low-level MCP server: ``list_tools`` + ``call_tool`` over ``adapter``."""
    server: Server[Any, Any] = Server("carve", version=__version__)
    tools = generate_tools_from_openapi(openapi_schema)

    # The SDK's decorator methods lack return annotations, so mypy --strict sees
    # them as untyped; scope-ignore rather than blanket-disable.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def _list_tools() -> list[Tool]:
        return tools

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            result = await adapter.call(name, arguments)
        except MCPToolError as error:
            return CallToolResult(
                content=[TextContent(type="text", text=error.message)],
                structuredContent=error.data if isinstance(error.data, dict) else None,
                isError=True,
            )
        # Wrap every REST payload ourselves: dicts get structuredContent + a text
        # rendering; lists/scalars get text only (the SDK's auto-wrap mishandles a
        # bare JSON array, so we never hand it one).
        text = json.dumps(result, indent=2, default=str)
        structured = result if isinstance(result, dict) else None
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=structured,
            isError=False,
        )

    return server


async def run_stdio(server: Server[Any, Any]) -> None:
    """Serve over stdio until stdin closes (the Claude Desktop / Cursor / Claude Code shape).

    stdout is the JSON-RPC channel here — this coroutine must never write to it
    except through the SDK. All logging is configured to stderr by the caller.
    """
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _build_security_settings(host: str, port: int) -> TransportSecuritySettings:
    """DNS-rebinding protection: only accept Host/Origin for the bound + loopback names.

    A rebinding browser page is served from the attacker's domain, so it sends
    that domain as ``Host`` — which is not in this allow-list, so the SDK rejects
    it (421). Legitimate loopback/configured clients match. Origin is absent for
    programmatic clients (allowed); a browser sends its real Origin (rejected).
    """
    from mcp.server.transport_security import TransportSecuritySettings

    names = [host, "127.0.0.1", "localhost", "[::1]"]
    hosts: list[str] = []
    origins: list[str] = []
    for name in names:
        for candidate in (f"{name}:{port}", f"{name}:*"):
            if candidate not in hosts:
                hosts.append(candidate)
        for scheme in ("http", "https"):
            for origin in (f"{scheme}://{name}:{port}", f"{scheme}://{name}:*"):
                if origin not in origins:
                    origins.append(origin)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def _extract_bearer(scope: Scope) -> str:
    """Pull the ``Authorization: Bearer <token>`` value from an ASGI scope, or ``""``."""
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            text = bytes(value).decode("latin-1")
            if text[:7].lower() == "bearer ":
                return text[7:].strip()
    return ""


async def _send_unauthorized(send: Send) -> None:
    """Emit a 401 JSON response that never echoes any token value."""
    body = b'{"error":"unauthorized","detail":"A valid bearer token is required."}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` equal to the server's configured token.

    Makes the http ``/mcp`` proxy no weaker than the REST API it fronts — the same
    bearer that authorizes REST is required to drive the MCP endpoint. Compared in
    constant time; the token is never logged or echoed.
    """

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        provided = _extract_bearer(scope)
        if not (provided and hmac.compare_digest(provided, self._token)):
            await _send_unauthorized(send)
            return
        await self._app(scope, receive, send)


async def run_http(
    server: Server[Any, Any], host: str, port: int, *, token: str, log_level: str = "warning"
) -> None:
    """Serve over the SDK's Streamable HTTP transport on a Starlette/uvicorn app.

    Hardened vs. a bare proxy: DNS-rebinding protection is on (Host/Origin
    allow-list for the bound + loopback names), and every request to ``/mcp`` must
    present the server's configured bearer token.
    """
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Mount

    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
        security_settings=_build_security_settings(host, port),
    )

    async def handle_streamable_http(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(
        routes=[Mount(HTTP_MOUNT_PATH, app=handle_streamable_http)],
        middleware=[Middleware(_BearerAuthMiddleware, token=token)],
        lifespan=lifespan,
    )
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level.lower())
    await uvicorn.Server(config).serve()


__all__ = ["HTTP_MOUNT_PATH", "build_server", "fetch_openapi_schema", "run_http", "run_stdio"]
