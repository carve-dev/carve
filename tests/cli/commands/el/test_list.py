"""Tests for `carve el list` (P1-07)."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from carve.cli.commands.el.list import render_el_list
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)


def _make_config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="el-list-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(snowflake={}),
        config_hash="cafef00dbeefcafe",
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / "targets" / "dev" / "el").mkdir(parents=True)
    (tmp_path / ".carve").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def repository(project_dir: Path) -> Repository:
    config = _make_config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


def _plant_artifact(project_dir: Path, *, target: str, name: str) -> None:
    artifact_dir = project_dir / "targets" / target / "el" / name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "main.py").write_text("print('x')\n")


def test_el_list_table_format(project_dir: Path, repository: Repository) -> None:
    """`carve el list` renders the documented columns: Name/Built/Last run/Status."""
    _plant_artifact(project_dir, target="dev", name="iowa_liquor_sales")
    _plant_artifact(project_dir, target="dev", name="salesforce_opps")

    repository.create_or_update_pipeline(
        name="iowa_liquor_sales",
        description="",
        pipeline_dir="targets/dev/el/iowa_liquor_sales",
    )
    # Seed a build to populate the "Built" column.
    repository.save_plan(_make_plan("plan_iowa", "iowa_liquor_sales"))  # type: ignore[arg-type]
    build = repository.create_build(
        pipeline_name="iowa_liquor_sales",
        plan_id="plan_iowa",
        target="dev",
    )
    repository.set_pipeline_current_build("iowa_liquor_sales", build.id)
    # Stamp a last_run_status so the Status column has content.
    run_id = repository.create_run(
        kind="run",
        target_id=build.id,
        pipeline_name="iowa_liquor_sales",
        target="dev",
    )
    repository.update_run_status(run_id, "success")
    repository.record_pipeline_run(
        pipeline_name="iowa_liquor_sales", run_id=run_id, status="success"
    )

    renderable = render_el_list(
        repository=repository,
        project_dir=project_dir,
        active_target="dev",
    )
    console = Console(record=True, width=140)
    console.print(renderable)
    output = console.export_text()
    assert "iowa_liquor_sales" in output
    assert "salesforce_opps" in output
    assert "Name" in output and "Built" in output and "Last run" in output and "Status" in output
    assert "success" in output
    # No-build artifact has "-" in built, "never" in last run.
    assert "never" in output


def test_el_list_empty_state(project_dir: Path, repository: Repository) -> None:
    """No artifacts → emit the documented empty-state message."""
    renderable = render_el_list(
        repository=repository,
        project_dir=project_dir,
        active_target="dev",
    )
    # The empty state is a string, not a table.
    assert isinstance(renderable, str)
    assert "No EL artifacts" in renderable
    assert "carve plan" in renderable


def _make_plan(plan_id: str, pipeline_name: str) -> object:
    from carve.core.state import Plan as _Plan

    return _Plan(
        id=plan_id,
        goal="g",
        config_hash="h",
        carve_version="0.0.1",
        task_graph_json="{}",
        file_path="x",
        phase="built",
        pipeline_name=pipeline_name,
    )
