"""``carve worker --once`` against a freshly-initialized Postgres.

Postgres-fixture-gated (``cli_env`` routes the spawned command at the per-test
Postgres). With one queued job the command runs it and exits 0; with an empty
queue it exits 0 cleanly. The job is a single creds-free sql step over an
in-process DuckDB connection, so the real ``build_step_executor_registry`` path
runs end-to-end without any warehouse credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.main import app
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue

runner = CliRunner()

_CARVE_TOML = """\
[project]
name = "worker-cli-test"
"""

# Connections live in the merged ``carve/connections.toml`` sub-file (top-level
# keys are the dialect blocks ``snowflake``/``duckdb``). A ``[duckdb.local]``
# block gives the creds-free in-process connector the sql step resolves.
_CONNECTIONS_TOML = """\
[duckdb.local]
path = ":memory:"
"""

_PIPELINE_TOML = """\
[pipeline]
description = "a single creds-free sql step"

[[steps]]
id = "refresh"
type = "sql"
file = "sql/refresh.sql"
connection = "local"
"""


def _project(tmp_path: Path) -> Path:
    (tmp_path / "carve.toml").write_text(_CARVE_TOML, encoding="utf-8")
    (tmp_path / "carve").mkdir()
    (tmp_path / "carve" / "connections.toml").write_text(_CONNECTIONS_TOML, encoding="utf-8")
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "ping.toml").write_text(_PIPELINE_TOML, encoding="utf-8")
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "refresh.sql").write_text("SELECT 1 AS ok", encoding="utf-8")
    return tmp_path


def _job_queue(database_url: str) -> JobQueue:
    from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
    from carve.core.config.state_store import StateStoreConfig

    config = Config(
        project=ProjectConfig(name="worker-cli-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=database_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return JobQueue(create_session_factory(engine))


def test_worker_once_with_empty_queue_exits_zero(
    tmp_path: Path,
    cli_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["worker", "--once"], env=cli_env)
    assert result.exit_code == 0, result.output
    assert "queue empty" in result.output


def test_worker_once_runs_a_queued_job_and_exits_zero(
    tmp_path: Path,
    cli_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.chdir(project)

    # Enqueue a job directly against the same per-test Postgres.
    queue = _job_queue(cli_env["DATABASE_URL"])
    job = queue.enqueue_manual("ping", "dev", trigger="manual")

    result = runner.invoke(app, ["worker", "--once"], env=cli_env)
    assert result.exit_code == 0, result.output
    assert "ran one job" in result.output

    # The job reached a terminal state and bound a run (the single sql step
    # succeeds over DuckDB).
    finished = queue.get_job(job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.run_id is not None
    repo = Repository(queue._session_factory)
    run = repo.get_run(finished.run_id)
    assert run is not None
    assert run.status == "success"


def test_worker_rejects_multiple_workers(
    tmp_path: Path,
    cli_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["worker", "--once", "--workers", "2"], env=cli_env)
    assert result.exit_code == 2
    assert "not supported" in result.output
