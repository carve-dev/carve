"""``carve mcp-serve`` — run Carve's MCP server (the SERVE side).

Spawned as a subprocess by Claude Desktop / Cursor / Claude Code (stdio), or run
as a long-lived Streamable HTTP endpoint (``--transport http``). It fetches the
REST OpenAPI schema, generates one MCP tool per non-streaming endpoint, and
translates every ``tools/call`` into a REST request.

**stdout is sacred in stdio mode:** it carries the JSON-RPC stream, so this
command routes *all* logging and human output to **stderr** and never prints to
stdout. A single stray stdout write corrupts the client handshake.

Not to be confused with ``carve mcp-servers`` (plural), the *consume*-side group
that registers external MCP servers. Heavy imports (the ``mcp`` SDK) are lazy so
plain ``carve`` invocations don't pay for them.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server

    from carve.mcp.adapter import RESTAdapter

# Every byte of human/diagnostic output goes to stderr so stdout stays a pure
# JSON-RPC channel in stdio mode.
err_console = Console(stderr=True)
logger = logging.getLogger(__name__)


def command(
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="Transport: 'stdio' (default; spawned by MCP clients) or 'http' "
        "(Streamable HTTP). 'ws' is a deprecated alias for 'http'.",
    ),
    port: int = typer.Option(
        8766, "--port", help="Port for the http transport (ignored for stdio)."
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Host for the http transport (ignored for stdio)."
    ),
    server_url: str = typer.Option(
        "http://127.0.0.1:8765",
        "--server-url",
        help="Carve REST API base URL.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        help="Bearer token override (default: $CARVE_API_TOKEN, then .carve/token).",
    ),
    log_level: str = typer.Option(
        "WARNING", "--log-level", help="Log level (to stderr): DEBUG/INFO/WARNING/ERROR."
    ),
) -> None:
    """Run Carve's MCP server, adapting the REST API into MCP tools.

    stdio runs until stdin closes; http runs until SIGTERM. Token discovery order:
    ``--token`` → ``$CARVE_API_TOKEN`` → ``.carve/token``.
    """
    # Logging → stderr, always. ``force`` overrides any handler a transitive
    # import installed, guaranteeing nothing logs to stdout in stdio mode.
    logging.basicConfig(
        stream=sys.stderr,
        level=_parse_level(log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    # Lazy: keep the mcp SDK + httpx off the cold ``carve`` import path (this
    # module is imported eagerly by the CLI, like the other command modules).
    import httpx

    from carve.core.config.paths import ProjectPaths
    from carve.mcp.adapter import RESTAdapter
    from carve.mcp.auth import MCPAuthError, resolve_token
    from carve.mcp.server import build_server, fetch_openapi_schema

    normalized = transport.strip().lower()
    if normalized == "ws":
        err_console.print(
            "[yellow]`ws` is deprecated; MCP uses Streamable HTTP — using `http`.[/yellow]"
        )
        normalized = "http"
    if normalized not in ("stdio", "http"):
        err_console.print(f"[red]Unknown transport {transport!r}. Use 'stdio' or 'http'.[/red]")
        raise typer.Exit(code=2)

    if normalized == "http" and host == "0.0.0.0":
        err_console.print(
            "[yellow]Binding the MCP HTTP transport to 0.0.0.0 exposes it on all "
            "interfaces — ensure this is intended.[/yellow]"
        )

    token_path = ProjectPaths.from_root(Path.cwd()).scratch_dir / "token"
    try:
        resolved_token = resolve_token(token, token_path=token_path)
    except MCPAuthError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    try:
        schema = fetch_openapi_schema(server_url, resolved_token)
    except (httpx.HTTPError, ValueError) as exc:
        # Never echo the token; the URL is safe to show. ``ValueError`` covers a
        # non-JSON / malformed body (e.g. an HTML error page from a wrong URL).
        err_console.print(
            f"[red]Could not load the Carve REST API schema from {server_url} "
            f"({type(exc).__name__}: {exc}). Is `carve serve` running and is "
            f"--server-url correct?[/red]"
        )
        raise typer.Exit(code=2) from exc

    adapter = RESTAdapter(base_url=server_url, token=resolved_token, openapi_schema=schema)
    server = build_server(adapter, schema)

    if normalized == "stdio":
        err_console.print("[green]carve mcp-serve[/green]: stdio transport ready.")
        asyncio.run(_serve_stdio(server, adapter))
    else:
        err_console.print(
            f"[green]carve mcp-serve[/green]: http transport on "
            f"http://{host}:{port}/mcp — requires the configured bearer token; "
            f"DNS-rebinding protection is on (Ctrl-C to stop)."
        )
        asyncio.run(_serve_http(server, adapter, host, port, log_level, resolved_token))


async def _serve_stdio(server: Server[Any, Any], adapter: RESTAdapter) -> None:
    from carve.mcp.server import run_stdio

    try:
        await run_stdio(server)
    finally:
        await adapter.aclose()


async def _serve_http(
    server: Server[Any, Any],
    adapter: RESTAdapter,
    host: str,
    port: int,
    log_level: str,
    token: str,
) -> None:
    from carve.mcp.server import run_http

    try:
        await run_http(server, host, port, token=token, log_level=log_level)
    finally:
        await adapter.aclose()


def _parse_level(level: str) -> int:
    resolved = logging.getLevelName(level.strip().upper())
    return resolved if isinstance(resolved, int) else logging.WARNING


__all__ = ["command"]
