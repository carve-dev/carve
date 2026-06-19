"""Tests for ``carve agents`` (list / show / create)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.main import app

runner = CliRunner()

_AGENT = """\
---
name: my-agent
description: A user agent for testing the CLI.
tools: [read_file, grep]
max_mode: read_only
classifications: [explore]
---
You are my-agent.
"""


def _write_user_agent(tmp_path: Path, name: str = "my-agent.md") -> None:
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / name).write_text(_AGENT, encoding="utf-8")


def test_agents_list_shows_user_agent(tmp_path: Path) -> None:
    _write_user_agent(tmp_path)
    result = runner.invoke(app, ["agents", "list", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "my-agent" in result.output
    assert "user" in result.output


def test_agents_show_prints_prompt(tmp_path: Path) -> None:
    _write_user_agent(tmp_path)
    result = runner.invoke(
        app, ["agents", "show", "my-agent", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "You are my-agent." in result.output
    assert "read_only" in result.output


def test_agents_show_unknown_fails(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["agents", "show", "ghost", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 1


_MALFORMED_AGENT = """\
---
name: broken
description: this file has an unknown max_mode
max_mode: not_a_real_mode
---
You are broken.
"""


def test_agents_show_surfaces_malformed_file_error(tmp_path: Path) -> None:
    """A file that exists but won't parse is reported as a load failure.

    Discovery silently skips a malformed file (one bad file must not break
    the rest); `carve agents show` re-attempts the load so the user sees
    *why* the agent is missing rather than a bare "unknown".
    """
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "broken.md").write_text(_MALFORMED_AGENT, encoding="utf-8")

    result = runner.invoke(
        app, ["agents", "show", "broken", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "failed to load" in result.output
    # And it is NOT reported as merely unknown.
    assert "No agent named" not in result.output


def test_agents_create_scaffolds_a_loadable_agent(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["agents", "create", "fresh", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    created = tmp_path / "carve" / "agents" / "fresh.md"
    assert created.is_file()

    # The scaffold is a working agent: it re-loads + shows.
    show = runner.invoke(
        app, ["agents", "show", "fresh", "--project-dir", str(tmp_path)]
    )
    assert show.exit_code == 0, show.output
    assert "fresh" in show.output


def test_agents_create_from_template(tmp_path: Path) -> None:
    _write_user_agent(tmp_path)
    result = runner.invoke(
        app,
        [
            "agents",
            "create",
            "copy",
            "--template",
            "my-agent",
            "--project-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    copy = tmp_path / "carve" / "agents" / "copy.md"
    assert copy.is_file()
    assert "name: copy" in copy.read_text(encoding="utf-8")


def test_agents_create_refuses_overwrite(tmp_path: Path) -> None:
    _write_user_agent(tmp_path)
    result = runner.invoke(
        app, ["agents", "create", "my-agent", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 1


@pytest.mark.parametrize("subcmd", ["list", "show", "create"])
def test_agents_help_is_available(subcmd: str) -> None:
    result = runner.invoke(app, ["agents", subcmd, "--help"])
    assert result.exit_code == 0
