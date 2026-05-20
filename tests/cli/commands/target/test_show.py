"""Tests for ``carve target show``."""

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


def test_target_show_uses_from_var_for_substituted(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_env: dict[str, str]
) -> None:
    """Substituted values render as ``<from <VAR_NAME>>``, never the secret."""
    _init_project(runner, tmp_path, cli_env)
    monkeypatch.setenv("DEV_SNOWFLAKE_PASSWORD", "topsecret-shibboleth")

    result = runner.invoke(
        app,
        ["target", "show", "dev", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 0, result.output
    assert "<from DEV_SNOWFLAKE_PASSWORD>" in result.output
    # The actual secret value must NEVER appear in the output.
    assert "topsecret-shibboleth" not in result.output


def test_target_show_points_at_carve_el_list_for_artifacts(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """P1.1-01: target show no longer enumerates per-target EL artifacts —
    the flat layout means artifacts are shared across targets. The
    output points users at `carve el list` instead."""
    _init_project(runner, tmp_path, cli_env)

    result = runner.invoke(
        app,
        ["target", "show", "dev", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 0, result.output
    assert "carve el list" in result.output
    # No per-target artifact listing in the output.
    assert "iowa_liquor" not in result.output


def test_target_show_marks_default(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app,
        ["target", "show", "dev", "--project-dir", str(tmp_path)],
        env=cli_env,
    )
    assert result.exit_code == 0, result.output
    assert "Default:        yes" in result.output


def test_target_show_missing_target_exits_2(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app, ["target", "show", "ghost", "--project-dir", str(tmp_path)]
    , env=cli_env)
    assert result.exit_code == 2, result.output


def test_target_show_rejects_unsafe_name(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    _init_project(runner, tmp_path, cli_env)
    result = runner.invoke(
        app, ["target", "show", "../escape", "--project-dir", str(tmp_path)]
    , env=cli_env)
    assert result.exit_code == 2, result.output
