"""Integration: `generate_plan` routes a NL goal to a live engine, or falls back.

`carve plan "ingest the Stripe API"` classifies → routes to the dlt-engineer →
runs it live (over the creds-free DuckDB substrate) → returns a `PlanArtifact`
whose cost reflects the engine's `DelegationResult`. A goal that classifies to
no engine (the model returns an out-of-set / no label) falls back to the M1
monolithic plan flow unchanged.

These tests touch the state store, so they use the Postgres `repository`
fixture (which skips cleanly when Docker/testcontainers is unavailable).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from carve.cli.orchestrator.planner import PlanArtifact, generate_plan
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
)
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

# --------------------------------------------------------------------- fixtures


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="route-plan-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(snowflake={}),
        config_hash="0123456789abcdef",
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / "pipelines").mkdir()
    return tmp_path


@pytest.fixture
def repository(project_dir: Path, postgres_state_store_url: str) -> Repository:
    config = _config(postgres_state_store_url)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


# --------------------------------------------------------------------- helpers


def _usage(input_tokens: int = 100, output_tokens: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _tool_use(name: str, input_: dict[str, Any], tool_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _resp(content: list[Any], usage: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason="tool_use", usage=usage or _usage())


def _end_turn(text: str, usage: SimpleNamespace | None = None) -> SimpleNamespace:
    """A model response that stops with no tool call (prose only)."""
    return SimpleNamespace(content=[_text(text)], stop_reason="end_turn", usage=usage or _usage())


class _SequencedClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return next(self._responses)


# --------------------------------------------------------------------- routed path


def test_plan_routes_to_dlt_engineer_and_captures_design(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A dlt goal routes to the dlt-engineer in DESIGN capacity → a reviewable Plan.

    The engineer runs at plan/read authority and returns a DESIGN via
    `submit_result.outputs` (mode="design") — NOT authored files (so
    `files_changed` is correctly empty). The routed path captures that DESIGN:
    the Plan carries the proposed file manifest (`planned_files`), the design
    summary (strategy + design_summary), the impact analysis (dependencies +
    tables_created), and the runtime estimate composed from `expected_outputs`.
    The cost rolls up from the engine's real `DelegationResult`.
    """
    config = _config(postgres_state_store_url)

    client = _SequencedClient(
        [
            # 1) the classify call → a dlt label
            _resp([_tool_use("classify_goal", {"label": "new_pipeline"}, "c1")]),
            # 2) the dlt-engineer child loop (DESIGN capacity): read a bound
            #    tool, then submit a `mode:"design"` payload — no files authored.
            _resp(
                [_tool_use("existing_dlt_inspect", {"op": "list"}, "t1")],
                usage=_usage(input_tokens=4000, output_tokens=600),
            ),
            _resp(
                [
                    _tool_use(
                        "submit_result",
                        {
                            "status": "succeeded",
                            "summary": "designed stripe pipeline",
                            "outputs": {
                                "mode": "design",
                                "pipeline_name": "stripe",
                                "strategy": "dlt rest_api source, incremental on id",
                                "planned_files": [
                                    "el/stripe/__init__.py",
                                    "el/stripe/requirements.txt",
                                ],
                                "design_summary": "Ingest the Stripe API.",
                                "requirements": ["dlt"],
                                "dependencies": {"python": ["dlt>=0.4"]},
                                "expected_outputs": {
                                    "tables_created": ["stripe.charges"],
                                    "first_run_seconds": 90,
                                    "subsequent_run_seconds": 30,
                                },
                            },
                        },
                        "t2",
                    )
                ],
                usage=_usage(input_tokens=2000, output_tokens=300),
            ),
        ]
    )

    artifact = generate_plan(
        goal="ingest the Stripe API into the warehouse",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    assert isinstance(artifact, PlanArtifact)
    assert artifact.pipeline_name == "stripe"
    assert artifact.description == "Ingest the Stripe API."
    assert artifact.requirements == ["dlt"]

    # The Plan cost reflects the engine's DelegationResult (summed child usage),
    # not a separate plan-agent loop. The child loop ran two model calls.
    assert artifact.tokens_input == 6000
    assert artifact.tokens_output == 900

    design = artifact.design
    # A genuine reviewable design, not a hollow artifact: a non-empty proposed
    # file manifest + a design summary.
    assert design["mode"] == "design"
    assert design["planned_files"] == [
        "el/stripe/__init__.py",
        "el/stripe/requirements.txt",
    ]
    assert design["strategy"] == "dlt rest_api source, incremental on id"
    assert design["design_summary"] == "Ingest the Stripe API."
    # Impact analysis from dependencies + expected tables.
    assert design["impact"]["dependencies"] == {"python": ["dlt>=0.4"]}
    assert design["impact"]["tables_created"] == ["stripe.charges"]
    # Runtime estimate composed from expected_outputs duration hints.
    assert design["runtime_estimate"]["first_run_seconds"] == 90
    assert design["runtime_estimate"]["subsequent_run_seconds"] == 30
    assert design["runtime_estimate"]["summary"] is not None

    # Persisted as a drafted Plan row.
    row = repository.get_plan(artifact.id)
    assert row is not None
    assert row.phase == "drafted"


# --------------------------------------------------------------------- fallback path


def test_unclassifiable_goal_falls_back_to_m1_path(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A goal the classifier can't place falls back to the unchanged M1 flow.

    The classifier returns no tool call → GoalClassificationError → fallback.
    The M1 plan agent then submits a design via `submit_plan` exactly as before.
    """
    config = _config(postgres_state_store_url)

    design = {
        "pipeline_name": "csv_ingest",
        "description": "Daily CSV ingest.",
        "requirements": ["snowflake-connector-python"],
    }
    client = _SequencedClient(
        [
            # 1) classify call → prose, no tool call → unclassifiable → fall back
            _resp([_text("not sure")]),
            # 2) the M1 plan agent submits a design (the unchanged M1 path).
            _resp(
                [_tool_use("submit_plan", design, "p1")],
                usage=_usage(input_tokens=2000, output_tokens=400),
            ),
        ]
    )

    artifact = generate_plan(
        goal="do something ambiguous and unroutable",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    # The M1 path produced the plan from submit_plan, unchanged.
    assert artifact.pipeline_name == "csv_ingest"
    assert artifact.description == "Daily CSV ingest."
    assert artifact.tokens_input == 2000
    assert artifact.tokens_output == 400
    # No routed-engine markers on the design (it's the M1 submit_plan design).
    assert "engine_outputs" not in artifact.design


def test_routed_engine_failure_falls_back_to_m1_path(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A routed engineer that fails to produce a design must NOT persist a Plan.

    The goal classifies + routes to the dlt-engineer, but the child loop returns
    prose and stops without calling `submit_result`, so the `DelegationResult`
    status is "failed". The routed path must reject that — no hollow `drafted`
    Plan with a fabricated name — and fall back to the M1 path, which then
    submits a real design via `submit_plan`.
    """
    config = _config(postgres_state_store_url)

    design = {
        "pipeline_name": "stripe_fallback",
        "description": "Fallback design.",
        "requirements": ["snowflake-connector-python"],
    }
    client = _SequencedClient(
        [
            # 1) classify → a dlt label (so it routes to the engineer).
            _resp([_tool_use("classify_goal", {"label": "new_pipeline"}, "c1")]),
            # 2) the dlt-engineer child loop stops with prose, no submit_result
            #    → the runner returns DelegationResult(status="failed").
            _end_turn(
                "I am thinking about it but never finalize.",
                usage=_usage(input_tokens=3000, output_tokens=400),
            ),
            # 3) the M1 plan agent submits a real design (the fallback path).
            _resp(
                [_tool_use("submit_plan", design, "p1")],
                usage=_usage(input_tokens=2000, output_tokens=400),
            ),
        ]
    )

    artifact = generate_plan(
        goal="ingest the Stripe API into the warehouse",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    # The failed delegation did NOT become the Plan; the M1 path did.
    assert artifact.pipeline_name == "stripe_fallback"
    assert artifact.description == "Fallback design."
    # M1-path markers, not routed-engine ones.
    assert "engine_outputs" not in artifact.design
    assert artifact.design.get("mode") != "design"
    # Only the M1 plan was persisted — exactly one Plan row, in phase drafted.
    row = repository.get_plan(artifact.id)
    assert row is not None
    assert row.phase == "drafted"
