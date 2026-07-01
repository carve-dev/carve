"""Integration (auth): missing token exits cleanly; invalid token → structured error.

The missing-token case needs neither server nor Postgres (token resolution fails
before any connection), so it runs offline. The invalid-token case needs a live
REST server enforcing real auth (a real StateStore), so it uses the Postgres
fixture and skips cleanly when Docker is absent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from carve.api.main import create_app
from carve.mcp.adapter import MCPToolError, RESTAdapter
from tests.integration._api_support import (
    free_port,
    make_config,
    make_state_store,
    project_paths,
    running_server,
)


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CARVE_API_TOKEN", None)
    env["CARVE_NO_DOTENV"] = "1"
    return env


def test_missing_token_exits_with_clear_error(tmp_path: Path) -> None:
    """No --token, no env, no .carve/token → clean non-zero exit + friendly stderr."""
    proc = subprocess.run(
        [sys.executable, "-m", "carve.cli.main", "mcp-serve", "--transport", "stdio"],
        cwd=str(tmp_path),  # clean cwd: no .carve/token to discover
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "No Carve API token found" in proc.stderr
    # The friendly message points at the recovery commands and never leaks stdout.
    assert "carve auth" in proc.stderr
    assert proc.stdout == ""


async def test_invalid_token_surfaces_structured_tool_error(
    postgres_state_store_url: str, tmp_path: Path
) -> None:
    """A bad bearer token → REST 401 → structured MCPToolError (not a crash)."""
    port = free_port()
    store = make_state_store(postgres_state_store_url)
    store.tokens.create(scopes=["*"])  # a valid token exists, but we present a wrong one
    config = make_config(postgres_state_store_url, port=port)
    app = create_app(store, config, project_paths=project_paths(tmp_path))
    schema = app.openapi()

    with running_server(app, port) as base:
        adapter = RESTAdapter(
            base_url=base, token="definitely-not-a-real-token", openapi_schema=schema
        )
        try:
            with pytest.raises(MCPToolError) as excinfo:
                await adapter.call("plans_list", {})
        finally:
            await adapter.aclose()

    error = excinfo.value
    # RFC 9457 problem+json from the REST auth layer, surfaced structurally.
    assert error.data.get("status") == 401
    assert "token" in error.message.lower() or "401" in error.message
