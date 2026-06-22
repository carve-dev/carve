"""Tests for ``carve target create``."""

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


def test_target_create_appends_section_to_connections(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """``[snowflake.<new>]`` is appended with ``${<NAME>_*}`` placeholders."""
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        ["target", "create", "staging", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "connections.toml").read_text()
    # Prior dev section preserved.
    assert "[snowflake.dev]" in content
    assert 'account = "${DEV_SNOWFLAKE_ACCOUNT}"' in content
    # New staging section.
    assert "[snowflake.staging]" in content
    assert 'account = "${STAGING_SNOWFLAKE_ACCOUNT}"' in content
    assert 'user = "${STAGING_SNOWFLAKE_USER}"' in content
    assert 'password = "${STAGING_SNOWFLAKE_PASSWORD}"' in content


def test_target_create_appends_block_to_env_example(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        ["target", "create", "staging", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 0, result.output

    content = (tmp_path / ".env.example").read_text()
    assert "# === dev target ===" in content
    assert "# === staging target ===" in content
    assert "STAGING_SNOWFLAKE_ACCOUNT=" in content
    assert "STAGING_SNOWFLAKE_USER=" in content


def test_target_create_does_not_create_targets_dir(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """P1.1-01: ``carve target create staging`` adds the connections.toml
    section and .env.example block, but does NOT create ``targets/staging/``.
    EL artifacts live in the flat ``el/<name>/`` tree, target-agnostic."""
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        ["target", "create", "staging", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "targets").exists()


def test_target_create_refuses_existing_section(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """``carve init`` already created ``[snowflake.dev]``, so re-creating it
    without ``--force`` exits 2."""
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        ["target", "create", "dev", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 2, result.output
    assert "already exists" in result.output


def test_target_create_refuses_invalid_name(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        ["target", "create", "Staging", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 2, result.output
    assert "must match" in result.output


def test_target_create_force_overwrites(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        ["target", "create", "dev", "--force", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / "carve" / "connections.toml").read_text()
    # Still only one [snowflake.dev] section.
    assert content.count("[snowflake.dev]") == 1


def test_target_create_preserves_comments_round_trip(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """Adding a new target preserves comments + ordering of prior sections.

    Acceptance: tomlkit edits preserve comments + ordering on
    ``carve/connections.toml``.
    """
    _init_project(runner, tmp_path, cli_env)
    conn = tmp_path / "carve" / "connections.toml"
    # Add a comment at the top of the file the user might have written.
    original = conn.read_text()
    customised = "# my custom header\n# multi-line\n" + original
    conn.write_text(customised)

    result = runner.invoke(
        app,
        ["target", "create", "staging", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 0, result.output
    content = conn.read_text()
    assert "# my custom header" in content
    assert "# multi-line" in content
    # Prior dev section still byte-identical for its key lines.
    assert 'account = "${DEV_SNOWFLAKE_ACCOUNT}"' in content
