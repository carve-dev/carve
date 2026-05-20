"""Renderer tests for `carve pipelines` listing and detail views."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from carve.cli.orchestrator import (
    render_pipeline_detail,
    render_pipelines_table,
)
from carve.core.config.schema import (
    Config,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.state import Plan, Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)


@pytest.fixture
def repository(tmp_path: Path, postgres_state_store_url: str) -> Repository:
    config = Config(
        project=ProjectConfig(name="pipes-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(state_store=postgres_state_store_url),
    )
    engine = create_engine_from_config(config, project_dir=tmp_path)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


def _seed_plan(repository: Repository, plan_id: str, **overrides: object) -> Plan:
    defaults: dict[str, object] = {
        "id": plan_id,
        "goal": f"goal for {plan_id}",
        "config_hash": "h",
        "carve_version": "0.0.1",
        "task_graph_json": "{}",
        "file_path": f".carve/plans/{plan_id}.json",
    }
    defaults.update(overrides)
    plan = Plan(**defaults)
    repository.save_plan(plan)
    return plan


# ----------------------------------------------------------------- listing


def test_render_pipelines_empty_state(repository: Repository) -> None:
    """No pipelines yet → friendly empty-state message."""
    renderable = render_pipelines_table(repository)
    console = Console(record=True, width=120)
    console.print(renderable)
    out = console.export_text()
    assert "No pipelines yet" in out


def test_render_pipelines_table_shows_each_row(repository: Repository) -> None:
    _seed_plan(repository, "plan-1")
    _seed_plan(repository, "plan-2")
    repository.create_or_update_pipeline(
        name="alpha",
        description="Alpha pipeline.",
        pipeline_dir="el/alpha",
    )
    repository.create_or_update_pipeline(
        name="beta",
        description="Beta pipeline.",
        pipeline_dir="el/beta",
    )
    renderable = render_pipelines_table(repository)
    console = Console(record=True, width=120)
    console.print(renderable)
    out = console.export_text()
    assert "alpha" in out
    assert "beta" in out
    assert "Alpha pipeline." in out
    assert "Beta pipeline." in out


# ----------------------------------------------------------------- detail


def test_render_pipeline_detail_unknown_returns_exit_1(
    repository: Repository,
) -> None:
    renderable, exit_code = render_pipeline_detail(repository, "nope")
    assert exit_code == 1
    console = Console(record=True, width=120)
    console.print(renderable)
    assert "not found" in console.export_text().lower()


def test_render_pipeline_detail_shows_lineage_and_runs(
    repository: Repository,
) -> None:
    """Detail view: parent chain, current plan, descendants, recent runs."""
    _seed_plan(repository, "plan-A")
    _seed_plan(repository, "plan-B", parent_plan_id="plan-A")
    _seed_plan(repository, "plan-C", parent_plan_id="plan-B")
    _seed_plan(repository, "plan-D", parent_plan_id="plan-C")
    repository.create_or_update_pipeline(
        name="ingest",
        description="Daily ingest.",
        pipeline_dir="el/ingest",
    )
    # The lineage view resolves "current_plan" through the pinned Build.
    build = repository.create_build(
        pipeline_name="ingest",
        plan_id="plan-C",
        target="dev",
    )
    repository.set_pipeline_current_build("ingest", build.id)
    # A few runs.
    run_id = repository.create_run(
        kind="run",
        target_id="plan-C",
        pipeline_name="ingest",
    )
    repository.update_run_status(run_id, "running")
    repository.update_run_status(run_id, "success")
    repository.record_pipeline_run(
        pipeline_name="ingest",
        run_id=run_id,
        status="success",
    )

    renderable, exit_code = render_pipeline_detail(repository, "ingest")
    assert exit_code == 0
    console = Console(record=True, width=120)
    console.print(renderable)
    out = console.export_text()
    assert "ingest" in out
    assert "Daily ingest." in out
    # All four plans show up: ancestors, current, refinement.
    for pid in ("plan-A", "plan-B", "plan-C", "plan-D"):
        assert pid in out
    # The recent-runs table includes a success row.
    assert "success" in out


def test_render_pipeline_detail_no_runs_yet(repository: Repository) -> None:
    """A freshly-built pipeline with no runs yet renders without errors."""
    _seed_plan(repository, "plan-1")
    repository.create_or_update_pipeline(
        name="fresh",
        description="",
        pipeline_dir="el/fresh",
    )
    renderable, exit_code = render_pipeline_detail(repository, "fresh")
    assert exit_code == 0
    console = Console(record=True, width=120)
    console.print(renderable)
    out = console.export_text()
    assert "fresh" in out
    assert "No runs yet" in out
