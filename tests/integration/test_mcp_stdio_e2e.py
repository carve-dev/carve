"""stdio e2e: spawn ``carve mcp-serve`` and drive it over JSON-RPC on stdin/stdout.

Runs a real Carve REST app under uvicorn (over a ``MagicMock`` state store — no
Postgres), spawns ``carve mcp-serve`` as the client would, and hand-drives the
``initialize`` → ``tools/list`` → ``tools/call`` handshake by writing JSON-RPC
frames to stdin and reading them from stdout.

The tool call targets ``healthz`` (``GET /healthz``): unauthenticated and
DB-free, it exercises the real adapter → REST → adapter path without any Postgres
setup, so this test does not require Docker.

**The load-bearing assertion:** every byte the subprocess wrote to stdout is a
well-formed JSON-RPC frame — a single stray ``print`` / log line to stdout would
corrupt the client handshake, and this test catches it.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import mcp.types as mcp_types

from carve.api.main import create_app
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig
from tests.integration._api_support import free_port, project_paths, running_server

_READ_TIMEOUT = 20.0


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CARVE_API_TOKEN", None)  # force the --token path; never inherit a token
    env["CARVE_NO_DOTENV"] = "1"
    return env


def _write(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _read_line(proc: subprocess.Popen[str], collected: list[str]) -> dict[str, Any]:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], _READ_TIMEOUT)
    if not ready:
        raise AssertionError("mcp-serve produced no stdout frame within the timeout")
    line = proc.stdout.readline()
    assert line, "mcp-serve closed stdout unexpectedly"
    collected.append(line)
    return json.loads(line)


def test_stdio_initialize_list_and_call_with_pure_stdout(tmp_path: Path) -> None:
    port = free_port()
    config = Config(
        project=ProjectConfig(name="mcp-stdio-e2e"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
    )
    app = create_app(MagicMock(), config, project_paths=project_paths(tmp_path))

    with running_server(app, port) as base:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "carve.cli.main",
                "mcp-serve",
                "--transport",
                "stdio",
                "--server-url",
                base,
                "--token",
                "dummy-token",
                "--log-level",
                "error",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(tmp_path),
            env=_clean_env(),
        )
        stdout_lines: list[str] = []
        try:
            # initialize
            _write(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": mcp_types.LATEST_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "e2e-client", "version": "0"},
                    },
                },
            )
            init_response = _read_line(proc, stdout_lines)
            assert init_response["id"] == 1
            assert init_response["result"]["serverInfo"]["name"] == "carve"
            assert "protocolVersion" in init_response["result"]

            # initialized notification (no response expected)
            _write(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

            # tools/list
            _write(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            list_response = _read_line(proc, stdout_lines)
            tool_names = {t["name"] for t in list_response["result"]["tools"]}
            assert "plans_list" in tool_names
            assert "healthz" in tool_names
            assert not any("stream" in n for n in tool_names)

            # tools/call → healthz (DB-free, exercises the real adapter → REST path)
            _write(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "healthz", "arguments": {}},
                },
            )
            call_response = _read_line(proc, stdout_lines)
            result = call_response["result"]
            assert result.get("isError") is not True
            assert result["structuredContent"] == {"status": "ok"}
        finally:
            if proc.stdin is not None:
                proc.stdin.close()

        try:
            returncode = proc.wait(timeout=_READ_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            stderr = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"mcp-serve did not exit after stdin close; stderr:\n{stderr}"
            ) from None

        # Drain any trailing stdout, then assert the WHOLE stdout stream was pure
        # JSON-RPC — no stray print/log ever reached the client channel.
        assert proc.stdout is not None
        remaining = proc.stdout.read()
        stderr_output = proc.stderr.read() if proc.stderr else ""

    all_lines = stdout_lines + [ln for ln in remaining.splitlines() if ln.strip()]
    assert len(all_lines) == 3, f"expected 3 JSON-RPC frames, got {len(all_lines)}: {all_lines}"
    for line in all_lines:
        frame = json.loads(line)  # raises if any stdout line is not JSON
        assert frame.get("jsonrpc") == "2.0", f"non-JSON-RPC stdout frame: {line!r}"

    assert returncode == 0, f"mcp-serve exited {returncode}; stderr:\n{stderr_output}"
