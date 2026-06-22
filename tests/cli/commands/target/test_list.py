"""Tests for ``carve target list``."""

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
    # init scaffolds the default `dev` target COMMENTED (so a fresh project
    # loads creds-free); these target-management tests need a LIVE `dev`, so
    # activate it explicitly — the same live section init used to write.
    result = runner.invoke(
        app, ["target", "create", "dev", "--project-dir", str(tmp_path)], env=cli_env
    )
    assert result.exit_code == 0, result.output


def test_target_list_marks_default(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_env: dict[str, str]
) -> None:
    _init_project(runner, tmp_path, cli_env)
    runner.invoke(
        app,
        ["target", "create", "staging", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    # Set DEV_* env vars so the secrets column doesn't matter for this test.
    for var in (
        "DEV_SNOWFLAKE_ACCOUNT",
        "DEV_SNOWFLAKE_USER",
        "DEV_SNOWFLAKE_PASSWORD",
        "DEV_SNOWFLAKE_ROLE",
        "DEV_SNOWFLAKE_WAREHOUSE",
        "DEV_SNOWFLAKE_DATABASE",
        "DEV_SNOWFLAKE_SCHEMA",
    ):
        monkeypatch.setenv(var, "x")

    result = runner.invoke(app, ["target", "list", "--project-dir", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output
    # The default is dev — there should be a `*` somewhere on the dev row.
    out = result.output
    assert "dev" in out
    assert "staging" in out
    assert "*" in out


def test_target_list_empty_state(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """No ``[snowflake.*]`` sections shows the empty-state message."""
    # Use a bare directory (no init).
    result = runner.invoke(app, ["target", "list", "--project-dir", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output
    assert "No targets yet" in result.output


def test_target_list_secrets_status(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_env: dict[str, str]
) -> None:
    """``Secrets`` column reports ``✗ missing`` when env vars aren't set, and
    ``✓ all set`` when they are."""
    _init_project(runner, tmp_path, cli_env)
    runner.invoke(app, ["target", "create", "staging", "--project-dir", str(tmp_path)], env=cli_env)

    # Only set DEV_* vars, leaving STAGING_* unset.
    for var in (
        "DEV_SNOWFLAKE_ACCOUNT",
        "DEV_SNOWFLAKE_USER",
        "DEV_SNOWFLAKE_PASSWORD",
        "DEV_SNOWFLAKE_ROLE",
        "DEV_SNOWFLAKE_WAREHOUSE",
        "DEV_SNOWFLAKE_DATABASE",
        "DEV_SNOWFLAKE_SCHEMA",
    ):
        monkeypatch.setenv(var, "x")
    for var in (
        "STAGING_SNOWFLAKE_ACCOUNT",
        "STAGING_SNOWFLAKE_USER",
        "STAGING_SNOWFLAKE_PASSWORD",
        "STAGING_SNOWFLAKE_ROLE",
        "STAGING_SNOWFLAKE_WAREHOUSE",
        "STAGING_SNOWFLAKE_DATABASE",
    ):
        monkeypatch.delenv(var, raising=False)

    result = runner.invoke(app, ["target", "list", "--project-dir", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output
    assert "all set" in result.output
    assert "missing" in result.output
