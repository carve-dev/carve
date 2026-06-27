"""``carve schedule list/show/pause/resume/set-cron`` against a per-test Postgres.

Postgres-fixture-gated (``cli_env`` routes the spawned command at the per-test
Postgres; the partial ``ix_schedules_due`` index + the CHECK are Postgres-only).
Each command mutates the live row + appends a ``schedule_changes`` audit row; a
bad cron / timezone exits 2 before any DB write. The deferred ``reseed`` stub is
covered by ``test_schedule_cli.py`` and stays untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.main import app
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.schedules import Schedules

runner = CliRunner()

_CARVE_TOML = """\
[project]
name = "schedule-cli-test"
"""


def _project(tmp_path: Path) -> Path:
    (tmp_path / "carve.toml").write_text(_CARVE_TOML, encoding="utf-8")
    (tmp_path / "carve").mkdir()
    (tmp_path / "carve" / "connections.toml").write_text("", encoding="utf-8")
    return tmp_path


def _schedules(database_url: str) -> Schedules:
    from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
    from carve.core.config.state_store import StateStoreConfig

    config = Config(
        project=ProjectConfig(name="schedule-cli-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=database_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return Schedules(create_session_factory(engine))


def test_set_cron_creates_and_audits(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(
        app,
        ["schedule", "set-cron", "sales", "*/5 * * * *", "--reason", "demo"],
        env=cli_env,
    )
    assert result.exit_code == 0, result.output
    assert "set cron" in result.output

    schedules = _schedules(cli_env["DATABASE_URL"])
    sched = schedules.get("sales")
    assert sched is not None
    assert sched.cron == "*/5 * * * *"
    assert sched.next_fires_at is not None
    changes = schedules.list_changes("sales")
    assert any(c.change_kind == "set_cron" and c.reason == "demo" for c in changes)


def test_set_cron_bad_cron_exits_two(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(app, ["schedule", "set-cron", "sales", "not a cron"], env=cli_env)
    assert result.exit_code == 2
    assert "Invalid cron" in result.output
    # Nothing was written.
    assert _schedules(cli_env["DATABASE_URL"]).get("sales") is None


def test_set_cron_bad_timezone_exits_two(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(
        app,
        ["schedule", "set-cron", "sales", "0 2 * * *", "--timezone", "Mars/Olympus"],
        env=cli_env,
    )
    assert result.exit_code == 2
    assert "Unknown timezone" in result.output


def test_set_cron_unsatisfiable_exits_two_no_write(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `0 0 30 2 *` (Feb 30) passes the grammar check but never matches a date;
    # the CLI exits 2 cleanly (no raw traceback) and persists nothing.
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(app, ["schedule", "set-cron", "sales", "0 0 30 2 *"], env=cli_env)
    assert result.exit_code == 2
    assert "unsatisfiable" in result.output.lower() or "never matches" in result.output.lower()
    assert _schedules(cli_env["DATABASE_URL"]).get("sales") is None


def test_pause_and_resume_mutate_and_audit(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    schedules = _schedules(cli_env["DATABASE_URL"])
    schedules.seed("sales", "*/5 * * * *", "dev")

    paused = runner.invoke(app, ["schedule", "pause", "sales", "--reason", "maint"], env=cli_env)
    assert paused.exit_code == 0, paused.output
    sched = schedules.get("sales")
    assert sched is not None and sched.paused is True and sched.paused_by == "user"
    assert any(
        c.change_kind == "pause" and c.reason == "maint" for c in schedules.list_changes("sales")
    )

    resumed = runner.invoke(app, ["schedule", "resume", "sales"], env=cli_env)
    assert resumed.exit_code == 0, resumed.output
    sched = schedules.get("sales")
    assert sched is not None and sched.paused is False and sched.paused_by is None


def test_pause_unknown_pipeline_exits_one(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(app, ["schedule", "pause", "ghost"], env=cli_env)
    assert result.exit_code == 1
    assert "No schedule" in result.output


def test_list_and_show_render(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    schedules = _schedules(cli_env["DATABASE_URL"])
    schedules.seed("sales", "*/5 * * * *", "dev")

    listed = runner.invoke(app, ["schedule", "list"], env=cli_env)
    assert listed.exit_code == 0, listed.output
    assert "sales" in listed.output

    shown = runner.invoke(app, ["schedule", "show", "sales"], env=cli_env)
    assert shown.exit_code == 0, shown.output
    assert "sales" in shown.output
    assert "*/5 * * * *" in shown.output


def test_list_empty_renders_message(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(app, ["schedule", "list"], env=cli_env)
    assert result.exit_code == 0, result.output
    assert "No schedules" in result.output
