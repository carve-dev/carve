"""``carve metrics costs/runs/agents`` against a per-test Postgres (CLI smoke).

Postgres-fixture-gated (``cli_env`` routes the spawned command at the per-test
Postgres). Seeds a run + an agent invocation + a skill call, then asserts each
metrics subcommand renders its rollup and exits 0; a bad ``--since`` exits 2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.main import app
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Agent, AgentInvocation, Pipeline, Run, SkillCall

runner = CliRunner()

_CARVE_TOML = """\
[project]
name = "metrics-cli-test"
"""


def _project(tmp_path: Path) -> Path:
    (tmp_path / "carve.toml").write_text(_CARVE_TOML, encoding="utf-8")
    (tmp_path / "carve").mkdir()
    (tmp_path / "carve" / "connections.toml").write_text("", encoding="utf-8")
    return tmp_path


def _seed(database_url: str) -> None:
    config = Config(
        project=ProjectConfig(name="metrics-cli-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=database_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    now = datetime.now(UTC)
    try:
        with session_factory() as session:
            session.add(Pipeline(name="sales", pipeline_dir="el/sales"))
            session.add(Agent(name="dlt-engineer"))
            session.add(
                Run(
                    id="r1",
                    kind="run",
                    target_id="b1",
                    pipeline_name="sales",
                    target="prod",
                    status="success",
                    duration_ms=150,
                    created_at=now,
                )
            )
            session.flush()
            session.add(
                AgentInvocation(
                    id="inv1",
                    agent_name="dlt-engineer",
                    run_id="r1",
                    tokens_input=1000,
                    tokens_output=200,
                    cost_usd=0.005,
                    duration_ms=1200,
                    status="succeeded",
                    started_at=now,
                )
            )
            session.add(SkillCall(agent_invocation_id="inv1", skill_name="edit_file"))
            session.commit()
    finally:
        engine.dispose()


def test_metrics_costs_renders(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    _seed(cli_env["DATABASE_URL"])
    result = runner.invoke(app, ["metrics", "costs", "--since", "30d"], env=cli_env)
    assert result.exit_code == 0, result.output
    assert "Cost" in result.output


def test_metrics_runs_renders(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    _seed(cli_env["DATABASE_URL"])
    result = runner.invoke(app, ["metrics", "runs", "--since", "30d"], env=cli_env)
    assert result.exit_code == 0, result.output
    assert "Runs" in result.output
    assert "sales" in result.output


def test_metrics_agents_renders(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    _seed(cli_env["DATABASE_URL"])
    # Widen the (non-tty) Rich console so the 12-char agent name is not truncated.
    result = runner.invoke(
        app, ["metrics", "agents", "--since", "30d"], env={**cli_env, "COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "dlt-engineer" in result.output


def test_metrics_agents_empty_renders_message(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    # No seed — the DB is migrated on first command invocation but has no rows.
    result = runner.invoke(app, ["metrics", "agents"], env=cli_env)
    assert result.exit_code == 0, result.output
    assert "No agent invocations" in result.output


def test_metrics_bad_since_exits_two(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(app, ["metrics", "costs", "--since", "soon"], env=cli_env)
    assert result.exit_code == 2
    assert "invalid --since" in result.output


def test_metrics_huge_since_exits_two_cleanly(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absurd ``--since`` overflows ``timedelta`` (OverflowError, not
    ValueError) — the CLI must still exit 2 cleanly, not surface a traceback (F5)."""
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(
        app, ["metrics", "costs", "--since", "9999999999999999999999d"], env=cli_env
    )
    assert result.exit_code == 2, result.output
    # No traceback leaked (the overflow was caught, not raised through).
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_metrics_markup_injection_since_exits_two_cleanly(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted ``--since`` with Rich markup (a stray closing tag) must be
    escaped, so the error path exits 2 cleanly instead of raising MarkupError (F4)."""
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(app, ["metrics", "costs", "--since", "[/x]"], env=cli_env)
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "invalid --since" in result.output
