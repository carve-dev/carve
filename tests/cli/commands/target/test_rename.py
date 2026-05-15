"""Tests for ``carve target rename``."""

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


def test_target_rename_renames_section(runner: CliRunner, tmp_path: Path) -> None:
    """``[snowflake.<old>]`` becomes ``[snowflake.<new>]``."""
    _init_project(runner, tmp_path)
    runner.invoke(
        app,
        ["target", "create", "staging", "--project-dir", str(tmp_path)],
    )

    result = runner.invoke(
        app,
        ["target", "rename", "staging", "qa", "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.staging]" not in content
    assert "[snowflake.qa]" in content
    assert 'account = "${QA_SNOWFLAKE_ACCOUNT}"' in content


def test_target_rename_renames_env_example_lines(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_project(runner, tmp_path)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)])

    result = runner.invoke(
        app,
        ["target", "rename", "staging", "qa", "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / ".env.example").read_text()
    assert "STAGING_SNOWFLAKE_ACCOUNT=" not in content
    assert "QA_SNOWFLAKE_ACCOUNT=" in content
    assert "# === qa target ===" in content


def test_target_rename_does_not_touch_targets_tree(
    runner: CliRunner, tmp_path: Path
) -> None:
    """P1.1-01: target rename operates only on configuration —
    connections.toml section, .env.example block, and (when matching)
    carve.toml's default_target. No filesystem rename under
    ``targets/`` (which no longer exists)."""
    _init_project(runner, tmp_path)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)])
    # P1.1-01: no targets/ tree is created by init or target create.
    assert not (tmp_path / "targets").exists()

    result = runner.invoke(
        app,
        ["target", "rename", "staging", "qa", "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    # Connection-config section flipped.
    conn = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.qa]" in conn
    assert "[snowflake.staging]" not in conn
    # No targets/ tree touched on either side.
    assert not (tmp_path / "targets").exists()


def test_target_rename_updates_default_target(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Renaming the default updates ``default_target`` in carve.toml."""
    _init_project(runner, tmp_path)

    result = runner.invoke(
        app,
        ["target", "rename", "dev", "qa", "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    carve_toml = (tmp_path / "carve.toml").read_text()
    assert 'default_target = "qa"' in carve_toml


def test_target_rename_refuses_if_destination_exists(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_project(runner, tmp_path)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)])

    result = runner.invoke(
        app,
        ["target", "rename", "staging", "dev", "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 2, result.output


def test_target_rename_refuses_invalid_new_name(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_project(runner, tmp_path)
    result = runner.invoke(
        app,
        ["target", "rename", "dev", "QA-1", "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 2, result.output


def test_target_rename_missing_old(runner: CliRunner, tmp_path: Path) -> None:
    _init_project(runner, tmp_path)
    result = runner.invoke(
        app,
        ["target", "rename", "ghost", "qa", "--project-dir", str(tmp_path)],
    )
    assert result.exit_code == 2, result.output


def test_target_rename_rejects_unsafe_old_name(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_project(runner, tmp_path)
    result = runner.invoke(
        app,
        [
            "target",
            "rename",
            "../escape",
            "qa",
            "--project-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2, result.output
