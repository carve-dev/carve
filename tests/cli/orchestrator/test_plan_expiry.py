"""Plan-expiry enforcement tests for `build_plan`.

`expires_at` is stamped by the planner (default 24h). `build_plan` rejects
a plan whose `expires_at` is in the past with `PlanExpiredError`. The
check uses an injectable `now` so tests force expiry deterministically.
The expiry gate sits AFTER the drift gate but BEFORE the agent runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.cli.orchestrator.builder import PlanExpiredError, build_plan
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
            input_tokens=10,
            output_tokens=5,
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


def _success_responses() -> tuple[Any, ...]:
    base = "el/csv_ingest"
    return (
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {"path": f"{base}/main.py", "content": "# gen\nprint('hi')\n"},
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {"path": f"{base}/requirements.txt", "content": "x\n"},
                    tool_id="tu_2",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done")], stop_reason="end_turn"),
    )


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="expiry-test"),
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


def _plant_plan(repository: Repository, *, plan_id: str, expires_at: datetime) -> Plan:
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
        expires_at=expires_at,
    )
    repository.save_plan(plan)
    return plan


def test_expired_plan_is_rejected(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A plan whose `expires_at` is in the past → PlanExpiredError."""
    config = _config(state_db=postgres_state_store_url)
    expired_at = datetime.now(UTC) - timedelta(hours=1)
    plan = _plant_plan(repository, plan_id="plan_20260101_000000_exp001", expires_at=expired_at)

    with pytest.raises(PlanExpiredError) as exc_info:
        build_plan(
            plan_id=plan.id,
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),  # never reached
        )
    assert "expired" in str(exc_info.value).lower()
    assert not (project_dir / "el" / "csv_ingest").exists()


def test_injected_now_forces_expiry(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A not-yet-expired plan is rejected when `now` is forced past its expiry."""
    config = _config(state_db=postgres_state_store_url)
    expires_at = datetime.now(UTC) + timedelta(hours=12)  # still valid by wall clock
    plan = _plant_plan(repository, plan_id="plan_20260101_000001_exp002", expires_at=expires_at)

    forced_now = expires_at + timedelta(minutes=1)
    with pytest.raises(PlanExpiredError):
        build_plan(
            plan_id=plan.id,
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),
            now=forced_now,
        )


def test_unexpired_plan_builds(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A plan well within its expiry window builds normally."""
    config = _config(state_db=postgres_state_store_url)
    expires_at = datetime.now(UTC) + timedelta(hours=24)
    plan = _plant_plan(repository, plan_id="plan_20260101_000002_exp003", expires_at=expires_at)

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses()),
    )
    assert artifact.success is True
