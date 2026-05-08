"""Unit tests for `cli.orchestrator.builder.build_plan`.

The Anthropic client is fully mocked; the build agent's `write_file`
tool is the real one, so each tool_use ends up writing a file under
`tmp_path/targets/<target>/el/<name>/`. The test then verifies that:

* `Pipeline` row is upserted.
* The plan's `phase` flips to "built".
* A `Build` row is created and `Pipeline.current_build_id` points at it.
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
    (tmp_path / "targets" / "dev" / "el").mkdir(parents=True)
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
        task_graph_json=json.dumps({"design": design, "pipeline_name": pipeline_name}),
        file_path=f"/tmp/{plan_id}.json",
        phase="drafted",
        pipeline_name=pipeline_name_on_row,
    )
    repository.save_plan(plan)
    return plan


def _success_responses(
    *,
    pipeline_name: str = "csv_ingest",
    target: str = "dev",
) -> tuple[Any, ...]:
    """Two write_file calls + an end_turn summary, scoped to the per-target dir."""
    base = f"targets/{target}/el/{pipeline_name}"
    return (
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": f"{base}/main.py",
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
                        "path": f"{base}/requirements.txt",
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
    """Build agent writes both files → Pipeline + Build rows created, plan built."""
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
    assert artifact.target == "dev"
    assert artifact.pipeline_dir == "targets/dev/el/csv_ingest"
    assert (project_dir / "targets/dev/el/csv_ingest" / "main.py").is_file()
    assert (project_dir / "targets/dev/el/csv_ingest" / "requirements.txt").is_file()

    # Build artifact carries the new build id.
    assert artifact.build_id is not None
    assert artifact.build_id.startswith("build_")

    # Pipeline row points at the new Build via current_build_id.
    pipeline = repository.get_pipeline("csv_ingest")
    assert pipeline is not None
    assert pipeline.current_build_id == artifact.build_id
    assert pipeline.description == "Daily ingest."
    assert pipeline.pipeline_dir == "targets/dev/el/csv_ingest"

    # Build row carries the plan + target binding and the manifest.
    build = repository.get_build(artifact.build_id)
    assert build is not None
    assert build.pipeline_name == "csv_ingest"
    assert build.plan_id == plan.id
    assert build.target == "dev"
    assert "main.py" in build.manifest_json
    assert "requirements.txt" in build.manifest_json

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
                        "path": "targets/dev/el/csv_ingest/requirements.txt",
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
    # No Build row should exist when the build agent returned without a main.py.
    assert artifact.build_id is None

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
                        "path": "targets/dev/el/csv_ingest/main.py",
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
                        "path": "targets/dev/el/csv_ingest/requirements.txt",
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
    rebuilt_main = (project_dir / "targets/dev/el/csv_ingest" / "main.py").read_text()
    assert "rebuild" in rebuilt_main


# ---------------------------------------------- existing-pipeline modification


def test_build_for_existing_pipeline_replaces_files(
    project_dir: Path, repository: Repository
) -> None:
    """Building a plan that targets an existing pipeline overwrites the files."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")

    # Plant the existing pipeline files + row under the per-target layout.
    pipeline_dir = project_dir / "targets" / "dev" / "el" / "csv_ingest"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "main.py").write_text("# old version\n")
    (pipeline_dir / "requirements.txt").write_text("snowflake-connector-python\n")
    seed_plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_seed00")
    repository.mark_plan_built(plan_id=seed_plan.id, pipeline_name="csv_ingest")
    repository.create_or_update_pipeline(
        name="csv_ingest",
        description="seed",
        pipeline_dir="targets/dev/el/csv_ingest",
    )
    seed_build = repository.create_build(
        pipeline_name="csv_ingest",
        plan_id=seed_plan.id,
        target="dev",
    )
    repository.set_pipeline_current_build("csv_ingest", seed_build.id)

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
    assert pipeline.current_build_id == artifact.build_id
    assert pipeline.current_build_id != seed_build.id  # bumped to the new build

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


# ---------------------------------------------------------------- per-target


def test_build_writes_to_active_target_path(
    project_dir: Path, repository: Repository
) -> None:
    """`carve build <id> --target staging` lands files under the staging dir."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000010_aaaaaa")

    client = _client_returning(*_success_responses(target="staging"))
    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
        target="staging",
    )
    assert artifact.success is True
    assert artifact.target == "staging"
    assert artifact.pipeline_dir == "targets/staging/el/csv_ingest"
    assert (project_dir / "targets/staging/el/csv_ingest/main.py").is_file()
    # Default `dev` target directory is untouched.
    assert not (project_dir / "targets/dev/el/csv_ingest/main.py").is_file()


def test_build_creates_build_row_with_correct_fields(
    project_dir: Path, repository: Repository
) -> None:
    """The Build row carries pipeline_name, plan_id, target, and a populated manifest."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000011_aaaaaa")

    client = _client_returning(*_success_responses())
    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    assert artifact.build_id is not None
    build = repository.get_build(artifact.build_id)
    assert build is not None
    assert build.pipeline_name == "csv_ingest"
    assert build.plan_id == plan.id
    assert build.target == "dev"
    # Manifest references the per-target paths the build wrote.
    assert "targets/dev/el/csv_ingest/main.py" in build.manifest_json
    assert "targets/dev/el/csv_ingest/requirements.txt" in build.manifest_json


def test_build_sets_current_build_id_on_pipeline(
    project_dir: Path, repository: Repository
) -> None:
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000012_aaaaaa")

    client = _client_returning(*_success_responses())
    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    pipeline = repository.get_pipeline("csv_ingest")
    assert pipeline is not None
    assert pipeline.current_build_id == artifact.build_id


def test_build_default_target_falls_through_to_config(
    project_dir: Path, repository: Repository
) -> None:
    """No --target → resolves to ``config.project.default_target``."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    config.project.default_target = "production"

    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000013_aaaaaa")
    client = _client_returning(*_success_responses(target="production"))
    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    assert artifact.success is True
    assert artifact.target == "production"
    assert (project_dir / "targets/production/el/csv_ingest/main.py").is_file()


def test_build_invalid_pipeline_name_rejected(
    project_dir: Path, repository: Repository
) -> None:
    """A design with a non-snake_case pipeline_name → BuildError before any IO."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(
        repository,
        plan_id="plan_20260101_000014_aaaaaa",
        pipeline_name="Bad-Name",
    )
    with pytest.raises(BuildError, match=r"valid pipeline name"):
        build_plan(
            plan_id=plan.id,
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),
        )


def test_two_builds_against_different_targets(
    project_dir: Path, repository: Repository
) -> None:
    """Two builds against dev/prod produce two Build rows; current_build_id ends at the latest."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000015_aaaaaa")

    artifact_dev = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses(target="dev")),
        target="dev",
    )
    assert artifact_dev.success is True

    # Second build against prod requires `--force` because the plan is
    # already in phase=built after the first build.
    artifact_prod = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses(target="prod")),
        target="prod",
        force=True,
    )
    assert artifact_prod.success is True
    assert artifact_dev.build_id != artifact_prod.build_id

    # Both build rows are reachable by (name, target) lookup.
    dev_build = repository.latest_build_for("csv_ingest", "dev")
    prod_build = repository.latest_build_for("csv_ingest", "prod")
    assert dev_build is not None and dev_build.id == artifact_dev.build_id
    assert prod_build is not None and prod_build.id == artifact_prod.build_id

    # Pipeline.current_build_id ends pointing at the most recent build.
    pipeline = repository.get_pipeline("csv_ingest")
    assert pipeline is not None
    assert pipeline.current_build_id == artifact_prod.build_id

    # Each target's directory got its own files.
    assert (project_dir / "targets/dev/el/csv_ingest/main.py").is_file()
    assert (project_dir / "targets/prod/el/csv_ingest/main.py").is_file()


# ---------------------------------------------------------------------------
# destination.toml emission + override application
# ---------------------------------------------------------------------------


def test_build_writes_destination_toml(
    project_dir: Path, repository: Repository
) -> None:
    """The build flow emits ``destination.toml`` next to ``main.py``.

    Stage-1 contract: per-artifact, per-target destination config.
    Table is always literal; database/schema match the connection
    defaults (ANALYTICS/RAW from the test config) so they appear
    commented-out in the file rather than as live overrides.
    """
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    # The default _config has dev's snowflake.database == "DB" / schema_ ==
    # None. The plan's design carries database="ANALYTICS", schema="RAW",
    # table="RAW_CSV". Because the plan's database differs from env,
    # destination.toml will write database as a live override.
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_destn1")

    client = _client_returning(*_success_responses())
    build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    dest_path = project_dir / "targets/dev/el/csv_ingest/destination.toml"
    assert dest_path.is_file()
    content = dest_path.read_text(encoding="utf-8")
    # Table is always live.
    assert 'table = "RAW_CSV"' in content
    # Database differs from env default → live override.
    live_db_lines = [
        line for line in content.splitlines() if line.startswith("database =")
    ]
    assert live_db_lines == ['database = "ANALYTICS"']


def test_build_destination_override_applies_before_agent_runs(
    project_dir: Path, repository: Repository
) -> None:
    """``destination_override`` mutates ``design.destination`` so the
    agent sees the user's chosen FQN AND destination.toml reflects it."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_destn2")

    client = _client_returning(*_success_responses())
    build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
        destination_override={"table": "OVERRIDDEN_TABLE", "schema": "STAGING"},
    )

    dest_path = project_dir / "targets/dev/el/csv_ingest/destination.toml"
    assert dest_path.is_file()
    content = dest_path.read_text(encoding="utf-8")
    assert 'table = "OVERRIDDEN_TABLE"' in content
    # schema=STAGING differs from default (test config has schema_=None) →
    # live override.
    live_schema_lines = [
        line for line in content.splitlines() if line.startswith("schema =")
    ]
    assert live_schema_lines == ['schema = "STAGING"']


def test_build_destination_override_empty_string_clears_field(
    project_dir: Path, repository: Repository
) -> None:
    """Passing ``""`` for a field via ``destination_override`` clears it
    from ``design.destination`` — the prompt-edit path uses this to
    revert an override back to "inherit from env."""
    config = _config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_destn3")

    client = _client_returning(*_success_responses())
    build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
        # The plan's design has database="ANALYTICS"; clear it so it
        # falls back to env at runtime.
        destination_override={"database": ""},
    )

    dest_path = project_dir / "targets/dev/el/csv_ingest/destination.toml"
    content = dest_path.read_text(encoding="utf-8")
    # database now matches env (None); should NOT be a live line.
    live_db_lines = [
        line for line in content.splitlines() if line.startswith("database =")
    ]
    assert live_db_lines == []
