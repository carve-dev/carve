"""Multi-engine BUILD authoring — `build_plan`'s B2 fork.

A Plan whose design carries `planned_by_engine` (a B1 multi-engine decomposition)
reconstructs the ordered `list[SubGoal]` and drives `run_engines` at
`parent_mode=BUILD` so each engineer authors its real slice in BUILD capacity. The
authored manifest the Build records is the UNION of every engine's harness-tracked
`DelegationResult.files_changed` — never a single `el/<name>/` dir snapshot.

These tests stub the `run_engines` and `run_review_fan_out` seams at the `builder`
module boundary (offline — no live LLM, mirroring B1's stub discipline) so the
fork's contract is asserted in isolation: which sub-goals are reconstructed, at
what mode, and what the manifest unions. The review gate's own behaviour is
covered in `test_build_review_gate.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from carve.cli.orchestrator import builder as builder_mod
from carve.cli.orchestrator.builder import BuildArtifact, build_plan
from carve.core.agents.delegation import DelegationResult
from carve.core.agents.loop import TokenUsage
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.review_fan_out import ReviewResult
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

# --------------------------------------------------------------------- fixtures


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="multi-engine-build-test"),
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


def _plant_multi_engine_plan(
    repository: Repository,
    *,
    plan_id: str,
    pipeline_name: str = "stripe",
) -> Plan:
    """A drafted Plan whose design carries a two-engine `planned_by_engine`."""
    design = {
        "mode": "design",
        "pipeline_name": pipeline_name,
        "description": "Ingest Stripe then stage it.",
        "planned_by_engine": [
            {
                "sub_goal": "ingest the Stripe API into the warehouse",
                "classification": "new_pipeline",
                "files": ["el/stripe/main.py", "el/stripe/requirements.txt"],
            },
            {
                "sub_goal": "stage the Stripe data with dbt",
                "classification": "new_model",
                "files": ["models/staging/stg_stripe.sql"],
            },
        ],
        "planned_files": [
            "el/stripe/main.py",
            "el/stripe/requirements.txt",
            "models/staging/stg_stripe.sql",
        ],
    }
    plan = Plan(
        id=plan_id,
        parent_plan_id=None,
        goal="ingest the Stripe API, then stage it with dbt",
        config_hash="0123456789abcdef",
        carve_version="0.0.1",
        task_graph_json={"design": design, "pipeline_name": pipeline_name},
        file_path=f"/tmp/{plan_id}.json",
        phase="drafted",
        pipeline_name=None,
    )
    repository.save_plan(plan)
    return plan


def _delegation(summary: str, files: list[str], *, status: str = "succeeded") -> DelegationResult:
    return DelegationResult(
        status=status,
        result_summary=summary,
        files_changed=files,
        outputs={},
        usage=TokenUsage(),
        cost_usd=0.0,
    )


def _clean_review() -> ReviewResult:
    return ReviewResult(findings=[], passed=True, by_reviewer={}, raw={})


# ------------------------------------------------------------ the multi-engine fork


def test_multi_engine_drives_engineers_at_build_capacity(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`planned_by_engine` → ordered SubGoals → `run_engines` at parent_mode=BUILD.

    Asserts the heart of B2's fork: the sub-goals are reconstructed in order, the
    delegation runs at BUILD (so engineers author), the runner is built once
    (`run_engines` guarantees this — we assert a single call), and the authored
    manifest is the UNION of both engines' `files_changed` (dlt's `el/**` AND
    dbt's `models/**` — a dir snapshot could not see both).
    """
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id="plan_20260101_000000_me0001")

    captured: dict[str, Any] = {}

    def _fake_run_engines(sub_goals: Any, **kwargs: Any) -> list[DelegationResult]:
        captured["sub_goals"] = list(sub_goals)
        captured["parent_mode"] = kwargs["parent_mode"]
        captured["run_engines_calls"] = captured.get("run_engines_calls", 0) + 1
        return [
            _delegation("authored stripe", ["el/stripe/main.py", "el/stripe/requirements.txt"]),
            _delegation("authored staging", ["models/staging/stg_stripe.sql"]),
        ]

    review_called: dict[str, Any] = {}

    def _fake_review(**kwargs: Any) -> ReviewResult:
        review_called["files_changed"] = list(kwargs["files_changed"])
        review_called["classifications"] = list(kwargs["classifications"])
        return _clean_review()

    monkeypatch.setattr(builder_mod, "run_engines", _fake_run_engines)
    monkeypatch.setattr(builder_mod, "run_review_fan_out", _fake_review)

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=object(),
    )

    assert isinstance(artifact, BuildArtifact)
    # Sub-goals reconstructed from planned_by_engine, IN ORDER.
    assert [s.classification for s in captured["sub_goals"]] == ["new_pipeline", "new_model"]
    assert captured["sub_goals"][0].sub_goal == "ingest the Stripe API into the warehouse"
    assert captured["sub_goals"][1].sub_goal == "stage the Stripe data with dbt"
    # Delegated in BUILD capacity (engineers author real files).
    assert captured["parent_mode"] == PermissionMode.BUILD
    # `run_engines` called exactly once (runner built once, sequential within).
    assert captured["run_engines_calls"] == 1

    # The authored manifest is the UNION of both engines' files_changed —
    # el/** AND models/** both present (a dir snapshot couldn't see both).
    assert artifact.files_written == [
        "el/stripe/main.py",
        "el/stripe/requirements.txt",
        "models/staging/stg_stripe.sql",
    ]
    # The review gate saw the same authored union and the authoring classifications.
    assert review_called["files_changed"] == artifact.files_written
    assert review_called["classifications"] == ["new_pipeline", "new_model"]

    # A clean review → the build persisted: Build row + current_build_id + built.
    assert artifact.success is True
    assert artifact.build_id is not None
    build = repository.get_build(artifact.build_id)
    assert build is not None
    assert build.manifest_json.get("files") == artifact.files_written
    pipeline = repository.get_pipeline("stripe")
    assert pipeline is not None
    assert pipeline.current_build_id == artifact.build_id
    plan_row = repository.get_plan(plan.id)
    assert plan_row is not None
    assert plan_row.phase == "built"


def test_failed_engine_does_not_persist_a_partial_build(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine that did not succeed fails the build cleanly — no Build row.

    Mirrors B1's "do not persist a partial": if any engine's DelegationResult is
    non-`succeeded`, the run row goes failed, no Build is written, the plan stays
    drafted, and the review gate is never reached.
    """
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id="plan_20260101_000000_me0002")

    def _fake_run_engines(sub_goals: Any, **kwargs: Any) -> list[DelegationResult]:
        return [
            _delegation("authored stripe", ["el/stripe/main.py"]),
            _delegation("ran out of turns", [], status="failed"),
        ]

    def _fail_if_called(**kwargs: Any) -> ReviewResult:
        raise AssertionError("review gate must not run when an engine failed")

    monkeypatch.setattr(builder_mod, "run_engines", _fake_run_engines)
    monkeypatch.setattr(builder_mod, "run_review_fan_out", _fail_if_called)

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=object(),
    )

    assert artifact.success is False
    assert artifact.build_id is None
    # The plan stays drafted — no partial materialization.
    plan_row = repository.get_plan(plan.id)
    assert plan_row is not None
    assert plan_row.phase == "drafted"
    # The build run row records the failure.
    run = repository.get_run(artifact.run_id)
    assert run is not None
    assert run.status == "failed"
