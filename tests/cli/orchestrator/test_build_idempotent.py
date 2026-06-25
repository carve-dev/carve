"""Idempotent-rebuild tests for `build_plan`.

Re-building the SAME plan against UNCHANGED config (with its recorded
file set still present on disk) is a no-op: `build_plan` returns the
existing build without re-running the agent or creating a duplicate
`Build` row, and `Pipeline.current_build_id` is unchanged. `--force`
still forces a true rebuild. A missing manifest file falls through to a
real rebuild.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.cli.orchestrator.builder import BuildError, build_plan
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

PLAN_HASH = "0123456789abcdef"


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, input_: dict[str, Any], tool_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _response(*, content: list[Any], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _client_returning(*responses: Any) -> MagicMock:
    client = MagicMock()
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        del kwargs
        return next(response_iter)

    client.messages.create.side_effect = _create
    return client


def _success_responses(content_marker: str = "v1") -> tuple[Any, ...]:
    base = "el/csv_ingest"
    return (
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {"path": f"{base}/main.py", "content": f"# {content_marker}\nprint('hi')\n"},
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
                        "path": f"{base}/requirements.txt",
                        "content": "snowflake-connector-python\n",
                    },
                    tool_id="tu_2",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done")], stop_reason="end_turn"),
    )


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="idem-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(snowflake={}),
        config_hash=PLAN_HASH,
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / "el").mkdir(parents=True)
    (tmp_path / ".carve" / "plans").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def repository(project_dir: Path, postgres_state_store_url: str) -> Repository:
    config = _config(state_db=postgres_state_store_url)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


def _plant_drafted_plan(repository: Repository, *, plan_id: str) -> Plan:
    design = {
        "pipeline_name": "csv_ingest",
        "description": "Daily ingest.",
        "destination": {"database": "ANALYTICS", "schema": "RAW", "table": "RAW_CSV"},
        "requirements": ["snowflake-connector-python"],
    }
    plan = Plan(
        id=plan_id,
        goal="ingest",
        config_hash=PLAN_HASH,
        carve_version="0.0.1",
        task_graph_json={"design": design, "pipeline_name": "csv_ingest"},
        file_path=f"/tmp/{plan_id}.json",
        phase="drafted",
    )
    repository.save_plan(plan)
    return plan


def _build_once(project_dir: Path, repository: Repository, config: Config, plan_id: str) -> Any:
    return build_plan(
        plan_id=plan_id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses("v1")),
    )


def test_rebuild_unchanged_config_is_noop(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """Second build of the same plan/config without --force returns the same build."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_idem01")

    first = _build_once(project_dir, repository, config, plan.id)
    assert first.success is True
    assert first.build_id is not None

    # An empty client: if the agent re-ran, `messages.create` would raise
    # StopIteration. The no-op must NOT touch the client.
    second = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(),  # zero responses — must not be called
    )
    assert second.success is True
    # Same build id; no new Build row.
    assert second.build_id == first.build_id
    assert second.run_id == ""  # no build run created for a no-op

    pipeline = repository.get_pipeline("csv_ingest")
    assert pipeline is not None
    assert pipeline.current_build_id == first.build_id

    # Exactly one build row exists for this pipeline/target.
    dev_build = repository.latest_build_for("csv_ingest", "dev")
    assert dev_build is not None
    assert dev_build.id == first.build_id


def test_force_still_rebuilds(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """`--force` opts out of the no-op and produces a fresh build."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000001_idem02")

    first = _build_once(project_dir, repository, config, plan.id)

    forced = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses("v2")),
        force=True,
    )
    assert forced.success is True
    assert forced.build_id != first.build_id  # a genuine new build
    assert forced.run_id != ""  # a real build run was created
    assert "v2" in (project_dir / "el" / "csv_ingest" / "main.py").read_text()


def test_missing_manifest_file_declines_noop_and_falls_to_phase_gate(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A recorded build file gone → the no-op short-circuit declines.

    The no-op is a SAFE NARROWING of the existing phase gate (it only
    fires when the recorded files are all present); it is never a
    widening that auto-rebuilds a dirty tree. With a manifest file
    missing, the short-circuit declines and the unchanged phase gate
    refuses the rebuild without `--force` — exactly as a built plan did
    before this slice. `--force` then performs a true rebuild.
    """
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000002_idem03")

    first = _build_once(project_dir, repository, config, plan.id)
    # Delete a manifest file so the no-op short-circuit can't reproduce it.
    (project_dir / "el" / "csv_ingest" / "main.py").unlink()

    # Without --force: not a no-op (file missing) AND the phase gate
    # refuses re-running the agent on an already-built plan.
    with pytest.raises(BuildError, match=r"already in phase"):
        build_plan(
            plan_id=plan.id,
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),  # never reached
        )

    # With --force: a genuine rebuild proceeds and restores the file.
    rebuilt = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses("v3")),
        force=True,
    )
    assert rebuilt.success is True
    assert rebuilt.run_id != ""  # the agent actually re-ran
    assert rebuilt.build_id != first.build_id
    assert "v3" in (project_dir / "el" / "csv_ingest" / "main.py").read_text()
