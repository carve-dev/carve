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
    result = runner.invoke(app, ["agents", "show", "my-agent", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "You are my-agent." in result.output
    assert "read_only" in result.output


def test_agents_show_unknown_fails(tmp_path: Path) -> None:
    result = runner.invoke(app, ["agents", "show", "ghost", "--project-dir", str(tmp_path)])
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

    result = runner.invoke(app, ["agents", "show", "broken", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "failed to load" in result.output
    # And it is NOT reported as merely unknown.
    assert "No agent named" not in result.output


def test_agents_create_scaffolds_a_loadable_agent(tmp_path: Path) -> None:
    result = runner.invoke(app, ["agents", "create", "fresh", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    created = tmp_path / "carve" / "agents" / "fresh.md"
    assert created.is_file()

    # The scaffold is a working agent: it re-loads + shows.
    show = runner.invoke(app, ["agents", "show", "fresh", "--project-dir", str(tmp_path)])
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
    result = runner.invoke(app, ["agents", "create", "my-agent", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1


# A file with a `---` fence but broken YAML: were it ever read by
# `_load_error_for`, the load error (and possibly its content) would be
# echoed to the console — the info-leak these guards close.
_OUT_OF_TREE_SECRET = """\
---
OUT_OF_TREE_SECRET_MARKER: "unterminated
---
body
"""


def test_agents_show_rejects_traversal_name(tmp_path: Path) -> None:
    """A `../../`-shaped name resolves outside the agents dir — not read.

    `carve agents show ../../secret` must report the agent as unknown
    rather than joining the name and loading (then echoing) a file outside
    the agents directory.
    """
    # Plant a malformed file outside the user agents dir (which is
    # `<tmp>/carve/agents`); `../../secret` from there lands at `<tmp>`.
    (tmp_path / "secret.md").write_text(_OUT_OF_TREE_SECRET, encoding="utf-8")

    result = runner.invoke(app, ["agents", "show", "../../secret", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1
    # Reported as unknown (name rejected) — NOT read-and-surfaced.
    assert "No agent named" in result.output
    assert "failed to load" not in result.output
    assert "OUT_OF_TREE_SECRET_MARKER" not in result.output


def test_agents_show_rejects_out_of_tree_symlink(tmp_path: Path) -> None:
    """An in-dir symlink whose target is out of tree is not followed-and-read.

    A safe-looking name (`evil`, no separators) that maps to a planted
    symlink `evil.md -> <out-of-tree>` must still report unknown: the
    post-resolve containment check rejects the resolved target.
    """
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "outside_secret.md"  # outside agents_dir
    secret.write_text(_OUT_OF_TREE_SECRET, encoding="utf-8")
    (agents_dir / "evil.md").symlink_to(secret)

    result = runner.invoke(app, ["agents", "show", "evil", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "No agent named" in result.output
    assert "failed to load" not in result.output
    assert "OUT_OF_TREE_SECRET_MARKER" not in result.output


def test_agents_create_rejects_traversal_name(tmp_path: Path) -> None:
    """`create ../../evil` must not write a file outside the agents dir."""
    result = runner.invoke(app, ["agents", "create", "../../evil", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Invalid agent name" in result.output
    # Nothing written outside the user agents dir.
    assert not (tmp_path / "evil.md").exists()


def test_agents_create_refuses_symlink_target(tmp_path: Path) -> None:
    """A planted (dangling) symlink at the target is not written through.

    The name is clean (`trap`), so the name guard passes; the write-side
    containment must still refuse to follow a symlink out of tree. A
    *dangling* link is the sharp case: `target.exists()` reports absent, so
    only the `is_symlink()` check stops `write_text` from creating the
    out-of-tree target.
    """
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.md"  # does NOT exist → dangling link
    (agents_dir / "trap.md").symlink_to(outside)

    result = runner.invoke(app, ["agents", "create", "trap", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "symlink" in result.output
    # write_text did not follow the link out of tree.
    assert not outside.exists()


# A valid, loadable agent placed OUT of tree: were discovery to follow an
# in-dir symlink to it, `list` would show it and `show <name>` would dump
# this body — the exfiltration the containment check closes.
_OUT_OF_TREE_AGENT = """\
---
name: leaked-agent
description: An out-of-tree agent that must not be discovered.
max_mode: read_only
---
DISCOVERY_BODY_MARKER should never appear in CLI output.
"""


def test_agents_discovery_does_not_follow_out_of_tree_symlink(
    tmp_path: Path,
) -> None:
    """A symlinked out-of-tree agent is neither listed nor shown (no leak)."""
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "leaked.md"  # outside the agents dir
    secret.write_text(_OUT_OF_TREE_AGENT, encoding="utf-8")
    (agents_dir / "evil.md").symlink_to(secret)

    listed = runner.invoke(app, ["agents", "list", "--project-dir", str(tmp_path)])
    assert "leaked-agent" not in listed.output
    assert "DISCOVERY_BODY_MARKER" not in listed.output

    shown = runner.invoke(app, ["agents", "show", "leaked-agent", "--project-dir", str(tmp_path)])
    assert shown.exit_code == 1
    assert "No agent named" in shown.output
    assert "DISCOVERY_BODY_MARKER" not in shown.output


@pytest.mark.parametrize("subcmd", ["list", "show", "create"])
def test_agents_help_is_available(subcmd: str) -> None:
    result = runner.invoke(app, ["agents", subcmd, "--help"])
    assert result.exit_code == 0
