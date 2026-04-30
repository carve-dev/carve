"""Tests for `cli.orchestrator.listing` (renderers for `runs` / `logs`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from carve.cli.orchestrator.listing import render_logs, render_runs_table
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
)
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)


def _config() -> Config:
    return Config(
        project=ProjectConfig(name="listing-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store="sqlite:///.carve/state.db"),
        connections=ConnectionsConfig(snowflake={}),
    )


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    config = _config()
    engine = create_engine_from_config(config, project_dir=tmp_path)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


# ------------------------------------------------------------ runs renderer


def test_runs_renders_empty_state_message(repository: Repository) -> None:
    rendered = render_runs_table(repository)
    console = Console(record=True, width=80)
    console.print(rendered)
    text = console.export_text()
    assert "No runs yet" in text


def test_runs_renders_table_with_populated_state(repository: Repository) -> None:
    run_id_a = repository.create_run(kind="apply", target_id="plan_aaa")
    run_id_b = repository.create_run(kind="apply", target_id="plan_bbb")
    repository.update_run_status(run_id_a, "running")
    repository.update_run_status(run_id_a, "success")
    repository.update_run_status(run_id_b, "running")
    repository.update_run_status(run_id_b, "failed", error="boom")

    rendered = render_runs_table(repository)
    console = Console(record=True, width=140)
    console.print(rendered)
    text = console.export_text()

    # Both run prefixes are rendered (8-char shorts)
    assert run_id_a[:8] in text
    assert run_id_b[:8] in text
    # Statuses appear (rich strips colour codes in `export_text`)
    assert "success" in text
    assert "failed" in text
    # Header row
    assert "Recent runs" in text


# ------------------------------------------------------------ logs renderer


def test_logs_prints_lines_for_existing_run(repository: Repository) -> None:
    run_id = repository.create_run(kind="apply", target_id="plan_xxx")
    repository.append_log(run_id, "info", "runner", "first line")
    repository.append_log(run_id, "warning", "runner", "second line")

    renderable, exit_code = render_logs(repository, run_id)
    assert exit_code == 0
    console = Console(record=True, width=120)
    console.print(renderable)
    text = console.export_text()
    assert "first line" in text
    assert "second line" in text
    assert "[info]" in text
    assert "[warning]" in text


def test_logs_handles_run_with_no_logs(repository: Repository) -> None:
    run_id = repository.create_run(kind="apply", target_id="plan_yyy")
    renderable, exit_code = render_logs(repository, run_id)
    assert exit_code == 0
    console = Console(record=True, width=120)
    console.print(renderable)
    assert "No logs recorded" in console.export_text()


def test_logs_errors_for_missing_run(repository: Repository) -> None:
    renderable, exit_code = render_logs(repository, "nonexistent-run")
    assert exit_code == 1
    console = Console(record=True, width=120)
    console.print(renderable)
    assert "not found" in console.export_text().lower()
