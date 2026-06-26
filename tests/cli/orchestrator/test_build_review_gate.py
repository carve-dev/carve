"""The review-gate contract — `build_plan` consuming the `ReviewResult` verdict.

After the multi-engine path authors, the live review fan-out's aggregate verdict
GATES the build. B2's contract:

* a `blocker`/`major` finding ⇒ BLOCK — no `Build` row / no `current_build_id`
  advance / the plan stays drafted; the artifact carries `success=False`,
  `review_passed=False`, and the blocking findings;
* a clean review ⇒ proceed — the `Build` row is written (with a `review` block
  in its manifest), `current_build_id` advances, `review_passed=True`;
* a `minor`/`info` finding ⇒ proceed (the gate passes) — the finding surfaces as
  a warning on the artifact, the build still records.

These stub `run_engines` (a clean authored slice) and `run_review_fan_out` (canned
verdicts) at the `builder` module boundary — offline, no live LLM.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.cli.orchestrator import builder as builder_mod
from carve.cli.orchestrator.builder import BuildError, build_plan
from carve.core.agents.delegation import DelegationResult
from carve.core.agents.loop import TokenUsage
from carve.core.agents.review_fan_out import (
    Finding,
    ReviewFanOutError,
    ReviewResult,
    Severity,
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

# --------------------------------------------------------------------- fixtures


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="review-gate-test"),
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


def _plant_multi_engine_plan(repository: Repository, *, plan_id: str) -> Plan:
    design = {
        "mode": "design",
        "pipeline_name": "stripe",
        "description": "Ingest Stripe.",
        "planned_by_engine": [
            {
                "sub_goal": "ingest the Stripe API",
                "classification": "new_pipeline",
                "files": ["el/stripe/main.py"],
            },
        ],
        "planned_files": ["el/stripe/main.py"],
    }
    plan = Plan(
        id=plan_id,
        parent_plan_id=None,
        goal="ingest the Stripe API",
        config_hash="0123456789abcdef",
        carve_version="0.0.1",
        task_graph_json={"design": design, "pipeline_name": "stripe"},
        file_path=f"/tmp/{plan_id}.json",
        phase="drafted",
        pipeline_name=None,
    )
    repository.save_plan(plan)
    return plan


def _authored_slice() -> list[DelegationResult]:
    return [
        DelegationResult(
            status="succeeded",
            result_summary="authored stripe",
            files_changed=["el/stripe/main.py"],
            outputs={},
            usage=TokenUsage(),
            cost_usd=0.0,
        )
    ]


def _review_with(*findings: Finding) -> ReviewResult:
    failing = {Severity.BLOCKER, Severity.MAJOR}
    passed = not any(f.severity in failing for f in findings)
    by_reviewer: dict[str, list[Finding]] = {}
    for f in findings:
        by_reviewer.setdefault(f.reviewer, []).append(f)
    return ReviewResult(findings=list(findings), passed=passed, by_reviewer=by_reviewer, raw={})


def _patch_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    review: ReviewResult,
) -> None:
    monkeypatch.setattr(builder_mod, "run_engines", lambda sub_goals, **kw: _authored_slice())
    monkeypatch.setattr(builder_mod, "run_review_fan_out", lambda **kw: review)


# ------------------------------------------------------------------- the gate


@pytest.mark.parametrize("severity", [Severity.BLOCKER, Severity.MAJOR])
def test_high_severity_finding_blocks_the_build(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
    severity: Severity,
) -> None:
    """A blocker/major finding BLOCKS — no Build row, verdict recorded on the artifact."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id=f"plan_20260101_0000_{severity.value[:6]}")

    blocker = Finding(
        reviewer="dlt-security",
        severity=severity,
        file="el/stripe/main.py",
        line=3,
        message="live credential literal committed",
        suggested_change='api_key = "${API_KEY}"',
    )
    _patch_seams(monkeypatch, review=_review_with(blocker))

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=object(),
    )

    # BLOCKED: no Build row, the build did not ship.
    assert artifact.success is False
    assert artifact.build_id is None
    assert artifact.review_passed is False
    assert artifact.review_blocking_count == 1
    # The blocking finding is carried on the artifact for the CLI to render.
    assert len(artifact.review_findings) == 1
    assert artifact.review_findings[0]["severity"] == severity.value
    assert artifact.review_findings[0]["reviewer"] == "dlt-security"

    # No Build row, no pipeline advance, plan stays drafted.
    pipeline = repository.get_pipeline("stripe")
    assert pipeline is None or pipeline.current_build_id is None
    plan_row = repository.get_plan(plan.id)
    assert plan_row is not None
    assert plan_row.phase == "drafted"
    # The run row records the gate failure.
    run = repository.get_run(artifact.run_id)
    assert run is not None
    assert run.status == "failed"


def test_clean_review_proceeds_and_records_build(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean review → the build records; `review_passed=True`; verdict on the manifest."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id="plan_20260101_000000_clean1")
    _patch_seams(monkeypatch, review=_review_with())  # no findings

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=object(),
    )

    assert artifact.success is True
    assert artifact.review_passed is True
    assert artifact.review_blocking_count == 0
    assert artifact.review_findings == []
    assert artifact.build_id is not None

    # The verdict lands on the Build manifest (a `review` block).
    build = repository.get_build(artifact.build_id)
    assert build is not None
    review_block = build.manifest_json.get("review")
    assert review_block is not None
    assert review_block["passed"] is True
    assert review_block["findings"] == []

    plan_row = repository.get_plan(plan.id)
    assert plan_row is not None
    assert plan_row.phase == "built"


def test_minor_finding_proceeds_as_warning(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `minor` finding does NOT block — the build records, the finding warns."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id="plan_20260101_000000_minor1")
    nit = Finding(
        reviewer="dlt-qa",
        severity=Severity.MINOR,
        file="el/stripe/main.py",
        message="missing provenance header",
    )
    _patch_seams(monkeypatch, review=_review_with(nit))

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=object(),
    )

    # Proceeds (the gate passes) — Build recorded.
    assert artifact.success is True
    assert artifact.build_id is not None
    # The gate passed (no blocker/major), but the minor finding still surfaces.
    assert artifact.review_passed is True
    assert artifact.review_blocking_count == 0
    assert len(artifact.review_findings) == 1
    assert artifact.review_findings[0]["severity"] == "minor"

    # The build is real and the warning rode onto the manifest's review block.
    build = repository.get_build(artifact.build_id)
    assert build is not None
    review_block = build.manifest_json.get("review")
    assert review_block is not None
    assert review_block["passed"] is True
    assert len(review_block["findings"]) == 1


def test_review_path_exception_marks_run_failed_and_surfaces_buildcleanly(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising review fails the run TERMINALLY and surfaces a clean BuildError.

    The engines have already authored and the run row is `running`; if the fan-out
    raises (a malformed reviewer payload → `ReviewFanOutError`, a routing miss, a
    DB error on persist), the build must (1) mark the run `failed` — never leave an
    orphaned `running` row — and (2) surface a `BuildError` (CLI-catchable) rather
    than let the raw exception escape as a traceback. The gate stays fail-CLOSED:
    no Build row is written, the plan stays drafted.
    """
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id="plan_20260101_000000_raise1")

    # Capture the run_id build_plan creates, so we can assert it ended terminal
    # even though build_plan raises (no artifact is returned to read it from).
    created_run_ids: list[str] = []
    real_create_run = repository.create_run

    def _spy_create_run(*args: object, **kwargs: object) -> str:
        run_id = real_create_run(*args, **kwargs)  # type: ignore[arg-type]
        created_run_ids.append(run_id)
        return run_id

    monkeypatch.setattr(repository, "create_run", _spy_create_run)
    monkeypatch.setattr(builder_mod, "run_engines", lambda sub_goals, **kw: _authored_slice())

    def _boom(**kw: object) -> ReviewResult:
        raise ReviewFanOutError("a reviewer returned a malformed payload")

    monkeypatch.setattr(builder_mod, "run_review_fan_out", _boom)

    with pytest.raises(BuildError) as excinfo:
        build_plan(
            plan_id=plan.id,
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=object(),
        )

    # The raw ReviewFanOutError is wrapped in a clean BuildError, chained.
    assert "review/persistence" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ReviewFanOutError)

    # The run row is TERMINAL (failed), not orphaned as "running".
    assert created_run_ids, "build_plan should have created a run"
    run = repository.get_run(created_run_ids[-1])
    assert run is not None
    assert run.status == "failed"

    # Fail-closed: no Build shipped, the plan stays drafted.
    pipeline = repository.get_pipeline("stripe")
    assert pipeline is None or pipeline.current_build_id is None
    plan_row = repository.get_plan(plan.id)
    assert plan_row is not None
    assert plan_row.phase == "drafted"
