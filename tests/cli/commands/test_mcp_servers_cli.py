"""Tests for ``carve mcp-servers`` (add / list / remove)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from carve.cli.main import app
from carve.core.mcp.config import McpServer, load_mcp_config

runner = CliRunner()


def test_add_then_list(tmp_path: Path) -> None:
    add = runner.invoke(
        app,
        [
            "mcp-servers",
            "add",
            "jira",
            "--command",
            "mcp-jira --stdio",
            "--project-dir",
            str(tmp_path),
        ],
    )
    assert add.exit_code == 0, add.output

    # The entry landed in carve/mcp.toml.
    config = load_mcp_config(tmp_path / "carve" / "mcp.toml")
    assert config.by_name("jira") is not None

    listed = runner.invoke(app, ["mcp-servers", "list", "--project-dir", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert "jira" in listed.output


def test_add_requires_endpoint(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mcp-servers", "add", "bad", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1


def test_remove(tmp_path: Path) -> None:
    runner.invoke(
        app,
        [
            "mcp-servers",
            "add",
            "jira",
            "--command",
            "x",
            "--project-dir",
            str(tmp_path),
        ],
    )
    removed = runner.invoke(app, ["mcp-servers", "remove", "jira", "--project-dir", str(tmp_path)])
    assert removed.exit_code == 0, removed.output
    assert load_mcp_config(tmp_path / "carve" / "mcp.toml").by_name("jira") is None


def test_remove_unknown_fails(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mcp-servers", "remove", "ghost", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1


def test_list_empty(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mcp-servers", "list", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "No MCP servers" in result.output


def test_server_name_with_colon_rejected_at_model() -> None:
    """A ':' in the server name would corrupt mcp:<server>:<tool> namespacing."""
    with pytest.raises(ValidationError, match="must not contain ':'"):
        McpServer(name="a:b", command="x --stdio")


def test_add_server_name_with_colon_fails(tmp_path: Path) -> None:
    """`carve mcp-servers add` rejects a colon in the server name."""
    result = runner.invoke(
        app,
        [
            "mcp-servers",
            "add",
            "bad:name",
            "--command",
            "x --stdio",
            "--project-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert ":" in result.output  # the error mentions the offending colon
    # Nothing was written.
    assert not (tmp_path / "carve" / "mcp.toml").exists()
