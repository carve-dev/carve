"""Integration: `generate_plan` decomposes a multi-step goal across N engines.

`carve plan "ingest the Stripe API, then stage it with dbt"` DECOMPOSES into an
ordered list of sub-goals, routes each to its engineer (dlt-engineer + the
dbt-engineer) in DESIGN capacity, and merges the N `DelegationResult`s into ONE
reviewable `PlanArtifact`:

* the proposed file manifests concatenate, **labeled** by sub-goal/engine
  (`planned_by_engine`), plus a flat `planned_files`;
* the impact's dependency hints **union** (a shared `python` key keeps both
  engines' pins — it is NOT last-writer-wins);
* the cost is the **SUM** across every engine's usage;
* the runtime first/subsequent halves sum across engines.

A *partial* failure — some sub-goals succeed, a later one fails — must NOT
persist a partial routed Plan; the whole goal falls back to the M1 path.

These tests touch the state store, so they use the Postgres `repository` fixture
(which skips cleanly when Docker/testcontainers is unavailable).
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
        project=ProjectConfig(name="decompose-plan-test"),
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


def _usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
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


def _resp(content: list[Any], usage: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason="tool_use", usage=usage)


def _end_turn(text: str, usage: SimpleNamespace) -> SimpleNamespace:
    """A model response that stops with no tool call (prose only)."""
    return SimpleNamespace(content=[_text(text)], stop_reason="end_turn", usage=usage)


class _SequencedClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return next(self._responses)


_DECOMPOSE_TWO_ENGINES = _tool_use(
    "decompose_goal",
    {
        "sub_goals": [
            {
                "sub_goal": "ingest the Stripe API into the warehouse",
                "classification": "new_pipeline",
            },
            {
                "sub_goal": "stage the Stripe data with dbt",
                "classification": "new_model",
            },
        ]
    },
    "d1",
)


# ------------------------------------------------------------ the multi-engine merge


def test_plan_decomposes_to_dlt_and_dbt_engineers(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A two-step goal → dlt-engineer + dbt-engineer → ONE merged Plan.

    Asserts the heart of B1: the labeled per-engine manifest, the flat
    concatenation, the dependency UNION (not last-writer-wins), the concatenated
    tables, the SUMMED cost, and the summed runtime. The per-engine token counts
    are deliberately distinct from their sum, so a cost assertion can only pass
    if both engines were summed — never if one value was taken.
    """
    config = _config(postgres_state_store_url)

    client = _SequencedClient(
        [
            # 1) decompose → two ordered sub-goals (dlt, then dbt).
            _resp([_DECOMPOSE_TWO_ENGINES], usage=_usage(50, 10)),
            # 2) the dlt-engineer child loop (DESIGN): submit a design, no files.
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
                                "dependencies": {
                                    "python": ["dlt>=0.4"],
                                    "system": ["libpq"],
                                },
                                "expected_outputs": {
                                    "tables_created": ["stripe.charges"],
                                    "first_run_seconds": 90,
                                    "subsequent_run_seconds": 30,
                                },
                            },
                        },
                        "t1",
                    )
                ],
                usage=_usage(4000, 600),
            ),
            # 3) the dbt-engineer child loop (DESIGN): submit a design, no files.
            _resp(
                [
                    _tool_use(
                        "submit_result",
                        {
                            "status": "succeeded",
                            "summary": "designed staging model",
                            "outputs": {
                                "mode": "design",
                                "pipeline_name": "stg_stripe",
                                "strategy": "stage stripe.charges into stg_stripe",
                                "planned_files": ["models/staging/stg_stripe.sql"],
                                "design_summary": "Stage the Stripe charges.",
                                "requirements": ["dbt-core"],
                                # Shares the `python` key with dlt → must UNION,
                                # not clobber; `system` is dlt-only → must survive.
                                "dependencies": {"python": ["dbt>=1.0"]},
                                "expected_outputs": {
                                    "tables_created": ["analytics.stg_stripe"],
                                    "first_run_seconds": 20,
                                    "subsequent_run_seconds": 10,
                                },
                            },
                        },
                        "t2",
                    )
                ],
                usage=_usage(1500, 250),
            ),
        ]
    )

    artifact = generate_plan(
        goal="ingest the Stripe API, then stage it with dbt",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    assert isinstance(artifact, PlanArtifact)
    design = artifact.design
    assert design["mode"] == "design"

    # --- labeled per-engine manifest: one entry per sub-goal, in order ---------
    planned_by_engine = design["planned_by_engine"]
    assert [e["classification"] for e in planned_by_engine] == ["new_pipeline", "new_model"]
    assert planned_by_engine[0]["sub_goal"] == "ingest the Stripe API into the warehouse"
    assert planned_by_engine[0]["files"] == [
        "el/stripe/__init__.py",
        "el/stripe/requirements.txt",
    ]
    assert planned_by_engine[1]["sub_goal"] == "stage the Stripe data with dbt"
    assert planned_by_engine[1]["files"] == ["models/staging/stg_stripe.sql"]

    # --- flat manifest: the concatenation across both engines ------------------
    assert design["planned_files"] == [
        "el/stripe/__init__.py",
        "el/stripe/requirements.txt",
        "models/staging/stg_stripe.sql",
    ]

    # --- dependency UNION (the regression guard): the shared `python` key keeps
    #     BOTH engines' pins, and the dlt-only `system` key survives. A
    #     last-writer-wins `.update` would drop `dlt>=0.4` and `libpq`. ---------
    assert design["impact"]["dependencies"] == {
        "python": ["dlt>=0.4", "dbt>=1.0"],
        "system": ["libpq"],
    }
    # --- tables_created concatenate across engines -----------------------------
    assert design["impact"]["tables_created"] == ["stripe.charges", "analytics.stg_stripe"]

    # --- requirements union (de-duped, both distinct here) ---------------------
    assert artifact.requirements == ["dlt", "dbt-core"]

    # --- cost == the SUM across BOTH engines' DelegationResult usage. The
    #     decompose call (50/10) is NOT in a DelegationResult, so it is excluded.
    #     4000+1500 and 600+250 — distinct from either engine alone. -----------
    assert artifact.tokens_input == 5500
    assert artifact.tokens_output == 850

    # --- runtime: the first/subsequent halves sum across engines ---------------
    assert design["runtime_estimate"]["first_run_seconds"] == 110
    assert design["runtime_estimate"]["subsequent_run_seconds"] == 40

    # --- both engines' raw outputs are retained for the human ------------------
    assert len(design["engine_outputs"]) == 2

    # Persisted as exactly one drafted Plan row.
    row = repository.get_plan(artifact.id)
    assert row is not None
    assert row.phase == "drafted"


# ------------------------------------------------------------ partial-failure fallback


def test_partial_engine_failure_falls_back_to_m1_path(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A LATER sub-goal failing must discard the whole routed plan, not persist a partial.

    The goal decomposes to dlt + dbt. The dlt-engineer SUCCEEDS, but the
    dbt-engineer returns prose and stops without `submit_result`, so its
    `DelegationResult` status is "failed". The merge must reject the entire
    routed result — no partial Plan carrying only the dlt design — and fall back
    to the M1 path, which submits a real design via `submit_plan`. This case
    cannot arise at N=1; it is the reason the fallback is checked across ALL
    results before any Plan is built.
    """
    config = _config(postgres_state_store_url)

    m1_design = {
        "pipeline_name": "stripe_m1_fallback",
        "description": "M1 fallback design.",
        "requirements": ["snowflake-connector-python"],
    }
    client = _SequencedClient(
        [
            # 1) decompose → dlt, then dbt.
            _resp([_DECOMPOSE_TWO_ENGINES], usage=_usage(50, 10)),
            # 2) the dlt-engineer SUCCEEDS with a design.
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
                                "planned_files": ["el/stripe/__init__.py"],
                                "design_summary": "Ingest the Stripe API.",
                            },
                        },
                        "t1",
                    )
                ],
                usage=_usage(4000, 600),
            ),
            # 3) the dbt-engineer FAILS: prose, no submit_result → status "failed".
            _end_turn("I am still thinking and never finalize.", usage=_usage(3000, 400)),
            # 4) the M1 plan agent submits a real design (the fallback path).
            _resp([_tool_use("submit_plan", m1_design, "p1")], usage=_usage(2000, 400)),
        ]
    )

    artifact = generate_plan(
        goal="ingest the Stripe API, then stage it with dbt",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    # The partial routed result did NOT become the Plan; the M1 path did. The
    # dlt design (which succeeded) is absent — no partial Plan was persisted.
    assert artifact.pipeline_name == "stripe_m1_fallback"
    assert artifact.description == "M1 fallback design."
    assert "engine_outputs" not in artifact.design
    assert "planned_by_engine" not in artifact.design
    assert artifact.design.get("mode") != "design"
    # The M1 cost, not the routed engines' — confirms no routed Plan leaked.
    assert artifact.tokens_input == 2000
    assert artifact.tokens_output == 400

    # Exactly one Plan row (the M1 one), in phase drafted.
    row = repository.get_plan(artifact.id)
    assert row is not None
    assert row.phase == "drafted"
