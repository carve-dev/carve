"""A single-engine / M1 Plan (no `planned_by_engine`) still builds via the M1 path.

B2's fork selects the multi-engine path ONLY when the design carries a non-empty
`planned_by_engine`. A Plan without it (an M1-style design, or a single-engine
routed plan that produced no per-engine decomposition) must still build through
the unchanged single-`AgentLoop` build path — the same fallback discipline B1
used at plan time. This test asserts the M1 path is taken (the multi-engine
`run_engines` seam is NEVER called) and the widened `BuildArtifact` carries a
clean default verdict (no live review ran on the M1 path).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.cli.orchestrator import builder as builder_mod
from carve.cli.orchestrator.builder import BuildArtifact, build_plan
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


def _usage(input_tokens: int = 100, output_tokens: int = 50) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _tool_use(name: str, input_: dict[str, Any], tool_id: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _text(text: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(type="text", text=text)


def _response(*, content: list[Any], stop_reason: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=_usage())


def _client_returning(*responses: Any) -> MagicMock:
    client = MagicMock()
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        return next(response_iter)

    client.messages.create.side_effect = _create
    return client


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="single-engine-build-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(snowflake={}),
        config_hash="0123456789abcdef",
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


def _plant_m1_plan(repository: Repository, *, plan_id: str) -> Plan:
    """A drafted Plan with an M1 design — NO `planned_by_engine` key."""
    design = {
        "pipeline_name": "csv_ingest",
        "description": "Daily ingest.",
        "destination": {"database": "ANALYTICS", "schema": "RAW", "table": "RAW_CSV"},
        "requirements": ["snowflake-connector-python"],
    }
    plan = Plan(
        id=plan_id,
        parent_plan_id=None,
        goal="ingest",
        config_hash="0123456789abcdef",
        carve_version="0.0.1",
        task_graph_json={"design": design, "pipeline_name": "csv_ingest"},
        file_path=f"/tmp/{plan_id}.json",
        phase="drafted",
        pipeline_name=None,
    )
    repository.save_plan(plan)
    return plan


def _m1_success_responses() -> tuple[Any, ...]:
    base = "el/csv_ingest"
    return (
        _response(
            content=[
                _tool_use(
                    "write_file",
                    {"path": f"{base}/main.py", "content": "# gen\nimport os\nprint('hi')\n"},
                    "tu_1",
                )
            ],
            stop_reason="tool_use",
        ),
        _response(
            content=[
                _tool_use(
                    "write_file",
                    {"path": f"{base}/requirements.txt", "content": "requests\n"},
                    "tu_2",
                )
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text("Built csv_ingest.")], stop_reason="end_turn"),
    )


# ----------------------------------------------------------------- the M1 path


def test_m1_plan_builds_via_unchanged_path(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `planned_by_engine` → the M1 single-AgentLoop path, multi-engine seam untouched."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_m1_plan(repository, plan_id="plan_20260101_000000_m1path")

    # The multi-engine seam must NEVER be reached on the M1 path.
    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("run_engines must not run on the M1 single-agent path")

    monkeypatch.setattr(builder_mod, "run_engines", _explode)
    monkeypatch.setattr(builder_mod, "run_review_fan_out", _explode)

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_m1_success_responses()),
    )

    assert isinstance(artifact, BuildArtifact)
    assert artifact.success is True
    # The M1 invariants still hold: files on disk, Build row, pipeline advance, built.
    assert (project_dir / "el/csv_ingest/main.py").is_file()
    assert artifact.build_id is not None
    pipeline = repository.get_pipeline("csv_ingest")
    assert pipeline is not None
    assert pipeline.current_build_id == artifact.build_id
    plan_row = repository.get_plan(plan.id)
    assert plan_row is not None
    assert plan_row.phase == "built"


def test_m1_artifact_carries_clean_default_verdict(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
) -> None:
    """The widened `BuildArtifact` defaults to a clean verdict on the M1 path.

    No live review runs on the single-agent path, so `review_passed` is True,
    `review_findings` is empty, and `review_blocking_count` is 0 — the dataclass
    defaults the existing single-agent tests rely on staying green.
    """
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_m1_plan(repository, plan_id="plan_20260101_000000_m1vrd1")

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_m1_success_responses()),
    )

    assert artifact.review_passed is True
    assert artifact.review_findings == []
    assert artifact.review_blocking_count == 0


def test_empty_planned_by_engine_falls_back_to_m1(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A design with an EMPTY `planned_by_engine` list still builds via M1.

    An empty list means "no engine decomposition" — the fork must treat it the
    same as the key being absent and take the M1 path, not run zero engineers.
    """
    config = _config(state_db=postgres_state_store_url)
    design = {
        "pipeline_name": "csv_ingest",
        "description": "Daily ingest.",
        "requirements": ["requests"],
        "planned_by_engine": [],  # empty → M1 fallback
    }
    plan = Plan(
        id="plan_20260101_000000_empty1",
        parent_plan_id=None,
        goal="ingest",
        config_hash="0123456789abcdef",
        carve_version="0.0.1",
        task_graph_json={"design": design, "pipeline_name": "csv_ingest"},
        file_path="/tmp/empty.json",
        phase="drafted",
        pipeline_name=None,
    )
    repository.save_plan(plan)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("empty planned_by_engine must not run the multi-engine path")

    monkeypatch.setattr(builder_mod, "run_engines", _explode)

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_m1_success_responses()),
    )
    assert artifact.success is True
    assert artifact.build_id is not None
