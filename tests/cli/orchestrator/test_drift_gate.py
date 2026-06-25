"""Config-hash drift gate regression tests for `build_plan`.

A Plan carries the `config_hash` it was generated against. `build_plan`
refuses to materialise a Plan whose `config_hash` no longer matches
current config — raising `ConfigDriftError` BEFORE the `--force`/phase
gate, so `--force` cannot smuggle a build past a moved config. The CLI
maps this to exit 3 (see `tests/cli/commands` for the exit-code test).
An unchanged-config build proceeds normally.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.cli.orchestrator.builder import ConfigDriftError, build_plan
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
    *, content: list[Any], stop_reason: str, usage: SimpleNamespace | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=usage if usage is not None else _usage(),
    )


def _client_returning(*responses: Any) -> MagicMock:
    client = MagicMock()
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        del kwargs
        return next(response_iter)

    client.messages.create.side_effect = _create
    return client


def _success_responses(pipeline_name: str = "csv_ingest") -> tuple[Any, ...]:
    base = f"el/{pipeline_name}"
    return (
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {"path": f"{base}/main.py", "content": "# gen\nimport os\nprint('hi')\n"},
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


def _config(state_db: str, *, config_hash: str = PLAN_HASH) -> Config:
    return Config(
        project=ProjectConfig(name="drift-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(snowflake={}),
        config_hash=config_hash,
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


def _plant_drafted_plan(repository: Repository, *, plan_id: str, config_hash: str) -> Plan:
    design = {
        "pipeline_name": "csv_ingest",
        "description": "Daily ingest.",
        "destination": {"database": "ANALYTICS", "schema": "RAW", "table": "RAW_CSV"},
        "requirements": ["snowflake-connector-python"],
    }
    plan = Plan(
        id=plan_id,
        goal="ingest",
        config_hash=config_hash,
        carve_version="0.0.1",
        task_graph_json={"design": design, "pipeline_name": "csv_ingest"},
        file_path=f"/tmp/{plan_id}.json",
        phase="drafted",
    )
    repository.save_plan(plan)
    return plan


def test_drift_refuses_build_when_config_hash_moved(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A plan planned at PLAN_HASH built against a moved config → ConfigDriftError."""
    plan = _plant_drafted_plan(
        repository, plan_id="plan_20260101_000000_drift1", config_hash=PLAN_HASH
    )
    moved_config = _config(state_db=postgres_state_store_url, config_hash="ffffffffffffffff")

    with pytest.raises(ConfigDriftError) as exc_info:
        build_plan(
            plan_id=plan.id,
            config=moved_config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),  # never called — gate fires first
        )
    assert exc_info.value.plan_hash == PLAN_HASH
    assert exc_info.value.current_hash == "ffffffffffffffff"
    assert "drifted" in str(exc_info.value).lower()
    # The build agent never ran (no files written).
    assert not (project_dir / "el" / "csv_ingest").exists()


def test_force_does_not_bypass_drift(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """`--force` overrides 'already built', NOT 'config moved' — drift still wins."""
    plan = _plant_drafted_plan(
        repository, plan_id="plan_20260101_000001_drift2", config_hash=PLAN_HASH
    )
    moved_config = _config(state_db=postgres_state_store_url, config_hash="aaaaaaaaaaaaaaaa")

    with pytest.raises(ConfigDriftError):
        build_plan(
            plan_id=plan.id,
            config=moved_config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),
            force=True,  # must NOT bypass the drift gate
        )


def test_unchanged_config_build_proceeds(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A build against the SAME config_hash proceeds normally to success."""
    plan = _plant_drafted_plan(
        repository, plan_id="plan_20260101_000002_nodrift", config_hash=PLAN_HASH
    )
    config = _config(state_db=postgres_state_store_url, config_hash=PLAN_HASH)

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses()),
    )
    assert artifact.success is True
    assert (project_dir / "el" / "csv_ingest" / "main.py").is_file()
