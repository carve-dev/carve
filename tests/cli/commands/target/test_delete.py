"""Tests for ``carve target delete``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _init_project(runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output


def test_target_delete_removes_section(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    _init_project(runner, tmp_path, cli_env)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)], env=cli_env)

    result = runner.invoke(
        app,
        [
            "target",
            "delete",
            "staging",
            "--yes",
            "--project-dir",
            str(tmp_path),
        ],
    env=cli_env,
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.staging]" not in content
    assert "[snowflake.dev]" in content


def test_target_delete_removes_env_example_block(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    _init_project(runner, tmp_path, cli_env)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)], env=cli_env)

    result = runner.invoke(
        app,
        [
            "target",
            "delete",
            "staging",
            "--yes",
            "--project-dir",
            str(tmp_path),
        ],
    env=cli_env,
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / ".env.example").read_text()
    assert "# === staging target ===" not in content
    assert "STAGING_SNOWFLAKE_ACCOUNT=" not in content


def test_target_delete_does_not_touch_targets_tree(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """P1.1-01: target delete operates only on connection config.

    If a legacy ``targets/<name>/`` directory exists from a pre-P1.1
    project, the delete leaves it in place — the user can ``rm -rf``
    it themselves.
    """
    _init_project(runner, tmp_path, cli_env)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)], env=cli_env)
    # Pre-create a stale targets/staging/ tree (legacy from pre-P1.1).
    legacy_dir = tmp_path / "targets" / "staging" / "el"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "sentinel.txt").write_text("legacy")

    result = runner.invoke(
        app,
        [
            "target",
            "delete",
            "staging",
            "--yes",
            "--project-dir",
            str(tmp_path),
        ],
    env=cli_env,
    )
    assert result.exit_code == 0, result.output
    # Connection-config section removed.
    conn = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.staging]" not in conn
    # Legacy targets/<name>/ tree is untouched.
    assert (legacy_dir / "sentinel.txt").is_file()


def test_target_delete_default_target_refused(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """Without ``--force --no-default-warning``, deleting the default exits 2."""
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        ["target", "delete", "dev", "--yes", "--project-dir", str(tmp_path)],
    env=cli_env,
    )
    assert result.exit_code == 2, result.output


def test_target_delete_default_target_force_succeeds(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """With ``--force --no-default-warning``, the default target can be deleted."""
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        [
            "target",
            "delete",
            "dev",
            "--yes",
            "--force",
            "--no-default-warning",
            "--project-dir",
            str(tmp_path),
        ],
    env=cli_env,
    )
    assert result.exit_code == 0, result.output


def test_target_delete_nonexistent(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        ["target", "delete", "ghost", "--yes", "--project-dir", str(tmp_path)],
    env=cli_env,
    )
    assert result.exit_code == 2, result.output


def test_target_delete_rejects_unsafe_name(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """A path-traversal-shaped name must be rejected before any filesystem op."""
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        [
            "target",
            "delete",
            "../escape",
            "--yes",
            "--force",
            "--project-dir",
            str(tmp_path),
        ],
    env=cli_env,
    )
    assert result.exit_code == 2, result.output
    assert "must match" in result.output or "must" in result.output
