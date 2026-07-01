"""http e2e (one happy path): drive ``carve mcp-serve --transport http`` via the SDK.

The remote transport is the SDK's Streamable HTTP (MCP never adopted WebSocket).
We run the REST app under uvicorn (``MagicMock`` store — no Postgres), spawn
``carve mcp-serve --transport http``, and connect with the SDK's own
``streamable_http_client`` + ``ClientSession`` to run initialize → tools/list →
tools/call (``healthz``, DB-free). The http transport is hardened: it requires
the configured bearer token (a second test asserts missing/wrong → 401) and has
DNS-rebinding protection on. stdio carries the thorough coverage; this proves the
http wiring end-to-end.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from carve.api.main import create_app
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig
from tests.integration._api_support import free_port, project_paths, running_server

_TOKEN = "dummy-token"


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CARVE_API_TOKEN", None)
    env["CARVE_NO_DOTENV"] = "1"
    return env


@contextmanager
def _mcp_http_server() -> Iterator[int]:
    """Run a MagicMock REST app + ``carve mcp-serve --transport http``; yield the mcp port."""
    rest_port = free_port()
    mcp_port = free_port()
    config = Config(
        project=ProjectConfig(name="mcp-http-e2e"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
    )
    app = create_app(MagicMock(), config, project_paths=project_paths(Path.cwd()))

    with running_server(app, rest_port) as base:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "carve.cli.main",
                "mcp-serve",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(mcp_port),
                "--server-url",
                base,
                "--token",
                _TOKEN,
                "--log-level",
                "error",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_clean_env(),
        )
        try:
            _wait_for_port("127.0.0.1", mcp_port)
            yield mcp_port
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _wait_for_port(host: str, port: int, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.1)
    raise AssertionError(f"mcp-serve http transport never bound {host}:{port}")


async def test_http_initialize_list_and_call_with_bearer() -> None:
    with _mcp_http_server() as mcp_port:
        url = f"http://127.0.0.1:{mcp_port}/mcp"
        # The http transport requires the configured bearer; pass it on the client.
        # follow_redirects handles the Mount's ``/mcp`` → ``/mcp/`` 307 (same-origin,
        # so the Authorization header is preserved), matching the SDK's default client.
        async with httpx.AsyncClient(
            headers={"authorization": f"Bearer {_TOKEN}"}, follow_redirects=True
        ) as auth_client:
            async with streamable_http_client(url, http_client=auth_client) as (
                read_stream,
                write_stream,
                _get_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    assert "plans_list" in names
                    assert "healthz" in names

                    result = await session.call_tool("healthz", {})
                    assert result.isError is False
                    assert result.structuredContent == {"status": "ok"}


async def test_http_rejects_missing_and_wrong_bearer() -> None:
    with _mcp_http_server() as mcp_port:
        url = f"http://127.0.0.1:{mcp_port}/mcp"
        async with httpx.AsyncClient() as client:
            # No Authorization header → 401 before any MCP routing.
            no_auth = await client.get(url)
            assert no_auth.status_code == 401
            assert _TOKEN not in no_auth.text  # never echo the token

            # Wrong token → 401 as well.
            wrong = await client.get(url, headers={"authorization": "Bearer nope"})
            assert wrong.status_code == 401
