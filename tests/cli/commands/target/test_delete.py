"""Tests for ``carve target delete``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _init_project(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_target_delete_removes_section(runner: CliRunner, tmp_path: Path) -> None:
    _init_project(runner, tmp_path)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)])

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
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.staging]" not in content
    assert "[snowflake.dev]" in content


def test_target_delete_removes_env_example_block(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_project(runner, tmp_path)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)])

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
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / ".env.example").read_text()
    assert "# === staging target ===" not in content
    assert "STAGING_SNOWFLAKE_ACCOUNT=" not in content


def test_target_delete_removes_artifacts_dir(runner: CliRunner, tmp_path: Path) -> None:
    _init_project(runner, tmp_path)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)])

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
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "targets" / "staging").exists()


def test_target_delete_default_target_refused(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Without ``--force --no-default-warning``, deleting the default exits 2."""
    _init_project(runner, tmp_path)
    result = runner.invoke(
        app,
        ["target", "delete", "dev", "--yes", "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 2, result.output


def test_target_delete_default_target_force_succeeds(
    runner: CliRunner, tmp_path: Path
) -> None:
    """With ``--force --no-default-warning``, the default target can be deleted."""
    _init_project(runner, tmp_path)
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
    )
    assert result.exit_code == 0, result.output


def test_target_delete_non_empty_refused(runner: CliRunner, tmp_path: Path) -> None:
    """A target with EL artifacts in it requires ``--force``."""
    _init_project(runner, tmp_path)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)])
    # Pop an artifact in to make the dir non-empty.
    (tmp_path / "targets" / "staging" / "el" / "iowa_liquor").mkdir()

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
    )
    assert result.exit_code == 2, result.output


def test_target_delete_nonexistent(runner: CliRunner, tmp_path: Path) -> None:
    _init_project(runner, tmp_path)
    result = runner.invoke(
        app,
        ["target", "delete", "ghost", "--yes", "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 2, result.output


def test_target_delete_rejects_unsafe_name(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A path-traversal-shaped name must be rejected before any filesystem op."""
    _init_project(runner, tmp_path)
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
    )
    assert result.exit_code == 2, result.output
    assert "must match" in result.output or "must" in result.output
