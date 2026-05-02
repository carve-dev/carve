"""Unit tests for `cli.orchestrator.builder.build_plan`.

The Anthropic client is fully mocked; the build agent's `write_file`
tool is the real one, so each tool_use ends up writing a file under
`tmp_path/pipelines/<name>/`. The test then verifies that:

* `Pipeline` row is upserted.
* The plan's `phase` flips to "built".
* The build run row is marked success/failed appropriately.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.cli.orchestrator.builder import (
    BuildArtifact,
    BuildError,
    build_plan,
)
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
)
from carve.core.state import Plan, Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

# ----------------------------------------------------------- response helpers


def _usage(input_tokens: int = 100, output_tokens: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, input_: dict[str, Any], tool_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _response(
    *,
    content: list[Any],
    stop_reason: str,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=usage if usage is not None else _usage(),
    )


def _client_returning(*responses: Any) -> MagicMock:
    client = MagicMock()
    snapshots: list[dict[str, Any]] = []
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        snapshots.append(copy.deepcopy(kwargs))
        return next(response_iter)

    client.messages.create.side_effect = _create
    client.calls = snapshots
    return client


# ---------------------------------------------------------------- Config / fix


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="builder-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(snowflake={}),
        config_hash="0123456789abcdef",
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / "pipelines").mkdir()
    (tmp_path / ".carve" / "plans").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def repository(project_dir: Path) -> Repository:
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


def _plant_drafted_plan(
    repository: Repository,
    *,
    plan_id: str,
    pipeline_name: str = "csv_ingest",
    pipeline_name_on_row: str | None = None,
    parent_plan_id: str | None = None,
) -> Plan:
    """Insert a Plan row that mirrors what the planner persists."""
    design = {
        "pipeline_name": pipeline_name,
        "description": "Daily ingest.",
        "destination": {
            "database": "ANALYTICS",
            "schema": "RAW",
            "table": "RAW_CSV",
            "primary_key": "ID",
        },
        "transformation": {
            "strategy": "merge_upsert",
            "rationale": "Idempotent reruns.",
        },
        "columns": [{"name": "ID", "type": "VARCHAR(50)", "nullable": False}],
        "requirements": ["snowflake-connector-python", "requests"],
        "tradeoffs": [],
        "open_questions": [],
    }
    plan = Plan(
        id=plan_id,
        parent_plan_id=parent_plan_id,
        goal="ingest",
        config_hash="0123456789abcdef",
        carve_version="0.0.1",
        estimates_json="{}",
        task_graph_json=json.dumps({"design": design, "pipeline_name": pipeline_name}),
        file_path=f"/tmp/{plan_id}.json",
        phase="drafted",
        pipeline_name=pipeline_name_on_row,
    )
    repository.save_plan(plan)
    return plan


def _success_responses(*, pipeline_name: str = "csv_ingest") -> tuple[Any, ...]:
    """Two write_file calls + an end_turn summary."""
    return (
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": f"pipelines/{pipeline_name}/main.py",
                        "content": (
                            "# generated\n"
                            "import os\n"
                            "import snowflake.connector\n"
                            "print('hi')\n"
                        ),
                    },
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
            usage=_usage(input_tokens=2000, output_tokens=400),
        ),
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": f"pipelines/{pipeline_name}/requirements.txt",
                        "content": "snowflake-connector-python\nrequests\n",
                    },
                    tool_id="tu_2",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(
            content=[_text_block(f"Built {pipeline_name}.")],
            stop_reason="end_turn",
        ),
    )


# ------------------------------------------------------------------- happy path


def test_build_writes_files_and_marks_plan_built(
    project_dir: Path, repository: Repository
) -> None:
    """Build agent writes both files → Pipeline row created, plan marked built."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_aaaaaa")

    client = _client_returning(*_success_responses())
    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    assert isinstance(artifact, BuildArtifact)
    assert artifact.success is True
    assert artifact.pipeline_name == "csv_ingest"
    assert (project_dir / "pipelines" / "csv_ingest" / "main.py").is_file()
    assert (project_dir / "pipelines" / "csv_ingest" / "requirements.txt").is_file()

    # Pipeline row.
    pipeline = repository.get_pipeline("csv_ingest")
    assert pipeline is not None
    assert pipeline.current_plan_id == plan.id
    assert pipeline.description == "Daily ingest."

    # Plan flipped to built.
    plan_row = repository.get_plan(plan.id)
    assert plan_row is not None
    assert plan_row.phase == "built"
    assert plan_row.pipeline_name == "csv_ingest"

    # Build-run row exists and was marked success.
    run = repository.get_run(artifact.run_id)
    assert run is not None
    assert run.kind == "build"
    assert run.status == "success"
    assert run.pipeline_name == "csv_ingest"


def test_build_design_is_inlined_into_system_prompt(
    project_dir: Path, repository: Repository
) -> None:
    """The design appears in the build agent's system prompt, including destination."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000001_aaaaaa")
    client = _client_returning(*_success_responses())

    build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    system = client.calls[0]["system"]
    assert "## Design" in system
    assert "ANALYTICS" in system  # database from the design
    assert "merge_upsert" in system  # strategy from the design


# ---------------------------------------------------------------- failure path


def test_build_marks_failed_when_main_py_not_written(
    project_dir: Path, repository: Repository
) -> None:
    """Agent writes only requirements.txt → run failed, plan stays drafted."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000002_aaaaaa")

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/csv_ingest/requirements.txt",
                        "content": "snowflake-connector-python\n",
                    },
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done sort of")], stop_reason="end_turn"),
    )
    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    assert artifact.success is False

    plan_row = repository.get_plan(plan.id)
    assert plan_row is not None
    assert plan_row.phase == "drafted"

    run = repository.get_run(artifact.run_id)
    assert run is not None
    assert run.status == "failed"


def test_build_refuses_built_plan_without_force(
    project_dir: Path, repository: Repository
) -> None:
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000003_aaaaaa")
    # Build it once.
    build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses()),
    )

    with pytest.raises(BuildError, match=r"already in phase"):
        build_plan(
            plan_id=plan.id,
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),
        )


def test_build_force_rebuilds_a_built_plan(
    project_dir: Path, repository: Repository
) -> None:
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000004_aaaaaa")
    build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses()),
    )

    rebuild_responses = (
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/csv_ingest/main.py",
                        "content": "# rebuilt\nprint('rebuild')\n",
                    },
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/csv_ingest/requirements.txt",
                        "content": "snowflake-connector-python\n",
                    },
                    tool_id="tu_2",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("rebuilt")], stop_reason="end_turn"),
    )
    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*rebuild_responses),
        force=True,
    )
    assert artifact.success is True
    rebuilt_main = (project_dir / "pipelines" / "csv_ingest" / "main.py").read_text()
    assert "rebuild" in rebuilt_main


# ---------------------------------------------- existing-pipeline modification


def test_build_for_existing_pipeline_replaces_files(
    project_dir: Path, repository: Repository
) -> None:
    """Building a plan that targets an existing pipeline overwrites the files."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")

    # Plant the existing pipeline files + row.
    pipeline_dir = project_dir / "pipelines" / "csv_ingest"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "main.py").write_text("# old version\n")
    (pipeline_dir / "requirements.txt").write_text("snowflake-connector-python\n")
    seed_plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_seed00")
    repository.mark_plan_built(
        plan_id=seed_plan.id,
        pipeline_name="csv_ingest",
        build_run_id="seed_build",
    )
    repository.create_or_update_pipeline(
        name="csv_ingest",
        description="seed",
        pipeline_dir="pipelines/csv_ingest",
        current_plan_id=seed_plan.id,
    )

    # Plant the modification plan, with `pipeline_name` already locked.
    new_plan = _plant_drafted_plan(
        repository,
        plan_id="plan_20260101_000005_aaaaaa",
        pipeline_name_on_row="csv_ingest",
        parent_plan_id=None,
    )

    client = _client_returning(*_success_responses())
    artifact = build_plan(
        plan_id=new_plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    assert artifact.success is True

    pipeline = repository.get_pipeline("csv_ingest")
    assert pipeline is not None
    assert pipeline.current_plan_id == new_plan.id

    main_content = (pipeline_dir / "main.py").read_text()
    # Should be the new content from `_success_responses()`.
    assert "import snowflake.connector" in main_content


def test_build_unknown_plan_raises(project_dir: Path, repository: Repository) -> None:
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    with pytest.raises(BuildError, match=r"not found"):
        build_plan(
            plan_id="plan_20260101_000099_aaaaaa",
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),
        )
