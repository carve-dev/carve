"""Integration: the ``post_build`` emit fires after a recorded Build.

Plan-build Unit 2 sub-slice C. `build_plan` (M1/single path) and
`_build_multi_engine` both fire the `post_build` lifecycle hook AFTER the
`Build` row is durably recorded, with a flat payload
(`pipeline_name`/`build_id`/`target`/`plan_id`/`files`), gated at BUILD.

These tests assert the EMIT contract at the builder boundary:

* it fires exactly once, after the Build, with the right payload (both paths);
* a blocked / failed / no-`main.py` build does NOT fire it (no Build recorded);
* the idempotent no-op rebuild does NOT fire it (no new Build);
* a real `carve/hooks.toml` `post_build` hook runs through the BUILD gate;
* the post-commit contract: a hook that RAISES at fire time is logged but the
  Build STANDS (run stays `success`, `build_id` present).

The payload-recording / fire-count assertions monkeypatch
`build_extensibility_post_build_hook` at the builder boundary (a real bash
hook cannot write a sentinel — the BUILD gate denies redirects/`touch`), so
the injected hook records the payload directly. The real-wiring path uses an
`echo` hook (allow-listed at BUILD, exit 0) to prove the genuine gate path.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.cli.orchestrator import builder as builder_mod
from carve.cli.orchestrator.builder import build_plan
from carve.core.agents.delegation import DelegationResult
from carve.core.agents.loop import TokenUsage
from carve.core.agents.review_fan_out import ReviewResult
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
)
from carve.core.hooks.runner import HookExecutionError
from carve.core.hooks.wiring import LifecycleHook
from carve.core.state import Plan, Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

# --------------------------------------------------------------- mock client


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
    snapshots: list[dict[str, Any]] = []
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        snapshots.append(copy.deepcopy(kwargs))
        return next(response_iter)

    client.messages.create.side_effect = _create
    client.calls = snapshots
    return client


def _success_responses(*, pipeline_name: str = "csv_ingest") -> tuple[Any, ...]:
    base = f"el/{pipeline_name}"
    return (
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": f"{base}/main.py",
                        "content": "# generated\nimport os\nimport snowflake.connector\n",
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
                    {"path": f"{base}/requirements.txt", "content": "requests\n"},
                    tool_id="tu_2",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done")], stop_reason="end_turn"),
    )


# ---------------------------------------------------------------- Config / fix


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="post-build-test"),
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


def _plant_drafted_plan(
    repository: Repository, *, plan_id: str, pipeline_name: str = "csv_ingest"
) -> Plan:
    design = {
        "pipeline_name": pipeline_name,
        "description": "Daily ingest.",
        "destination": {"database": "ANALYTICS", "schema": "RAW", "table": "RAW_CSV"},
        "requirements": ["requests"],
    }
    plan = Plan(
        id=plan_id,
        parent_plan_id=None,
        goal="ingest",
        config_hash="0123456789abcdef",
        carve_version="0.0.1",
        task_graph_json={"design": design, "pipeline_name": pipeline_name},
        file_path=f"/tmp/{plan_id}.json",
        phase="drafted",
        pipeline_name=None,
    )
    repository.save_plan(plan)
    return plan


def _plant_multi_engine_plan(
    repository: Repository, *, plan_id: str, pipeline_name: str = "stripe"
) -> Plan:
    design = {
        "mode": "design",
        "pipeline_name": pipeline_name,
        "description": "Ingest then stage.",
        "planned_by_engine": [
            {
                "sub_goal": "ingest the Stripe API",
                "classification": "new_pipeline",
                "files": ["el/stripe/main.py"],
            },
            {
                "sub_goal": "stage with dbt",
                "classification": "new_model",
                "files": ["models/staging/stg_stripe.sql"],
            },
        ],
        "planned_files": ["el/stripe/main.py", "models/staging/stg_stripe.sql"],
    }
    plan = Plan(
        id=plan_id,
        parent_plan_id=None,
        goal="ingest then stage",
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


class _RecordingHook:
    """A `LifecycleHook` that records every payload it is fired with."""

    def __init__(self, *, raises: bool = False) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.raises = raises

    def __call__(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)
        if self.raises:
            raise HookExecutionError("post_build hook blew up (post-commit)")


def _patch_hook(monkeypatch: pytest.MonkeyPatch, hook: LifecycleHook | None) -> dict[str, int]:
    """Patch the builder's post_build hook builder to return `hook`.

    Returns a dict counting how many times the builder was invoked (so a test
    can assert the emit point even built a hook on the negative paths — it is
    the *firing*, not the building, that the negative paths must skip).
    """
    counter = {"built": 0}

    def _fake_builder(**_kwargs: Any) -> LifecycleHook | None:
        counter["built"] += 1
        return hook

    monkeypatch.setattr(builder_mod, "build_extensibility_post_build_hook", _fake_builder)
    return counter


# =================================================================== M1 path


def test_m1_path_fires_post_build_after_recorded_build(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single / M1 build path fires post_build once, after the Build, with
    the right payload (pipeline_name, build_id, target, plan_id, files)."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_pb0001")
    recording = _RecordingHook()
    _patch_hook(monkeypatch, recording)

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses()),
    )

    assert artifact.success is True
    assert artifact.build_id is not None
    # Fired exactly once, AFTER the Build (build_id is in the payload).
    assert len(recording.payloads) == 1
    payload = recording.payloads[0]
    assert payload["pipeline_name"] == "csv_ingest"
    assert payload["build_id"] == artifact.build_id
    assert payload["target"] == "dev"
    assert payload["plan_id"] == plan.id
    assert any("main.py" in f for f in payload["files"])
    # The Build the payload names is real + current on the pipeline.
    build = repository.get_build(payload["build_id"])
    assert build is not None
    pipeline = repository.get_pipeline("csv_ingest")
    assert pipeline is not None and pipeline.current_build_id == artifact.build_id


def test_m1_no_main_py_does_not_fire_post_build(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build that writes no main.py records NO Build → post_build never fires."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_pb0002")
    recording = _RecordingHook()
    _patch_hook(monkeypatch, recording)

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(
            _response(
                content=[
                    _tool_use_block(
                        "write_file",
                        {"path": "el/csv_ingest/requirements.txt", "content": "requests\n"},
                        tool_id="tu_1",
                    ),
                ],
                stop_reason="tool_use",
            ),
            _response(content=[_text_block("oops")], stop_reason="end_turn"),
        ),
    )

    assert artifact.success is False
    assert artifact.build_id is None
    assert recording.payloads == []  # no Build recorded → no emit


def test_m1_idempotent_noop_does_not_fire_post_build(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op rebuild reuses the existing Build → no NEW materialization →
    post_build does not re-fire."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_pb0003")

    # First build fires once.
    first_hook = _RecordingHook()
    counter = _patch_hook(monkeypatch, first_hook)
    first = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses()),
    )
    assert len(first_hook.payloads) == 1
    builds_after_first = counter["built"]

    # Second build (unchanged config, files present) is a no-op: the emit path
    # is never reached (short-circuits before the success branch), so neither
    # the builder is invoked again nor the hook fired.
    second = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(),  # would StopIteration if the agent ran
    )
    assert second.build_id == first.build_id
    assert len(first_hook.payloads) == 1  # not re-fired
    assert counter["built"] == builds_after_first  # builder not re-invoked


def test_m1_raising_post_build_hook_is_logged_but_build_stands(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-commit contract: a post_build hook that RAISES is surfaced (logged)
    but the Build STANDS — the run stays success and build_id is present."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_pb0004")
    _patch_hook(monkeypatch, _RecordingHook(raises=True))

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses()),
    )

    # The raise did NOT roll the build back.
    assert artifact.success is True
    assert artifact.build_id is not None
    run = repository.get_run(artifact.run_id)
    assert run is not None
    assert run.status == "success"  # NOT flipped to failed
    build = repository.get_build(artifact.build_id)
    assert build is not None  # the Build was not deleted
    pipeline = repository.get_pipeline("csv_ingest")
    assert pipeline is not None and pipeline.current_build_id == artifact.build_id


def test_m1_real_hooks_toml_fires_post_build_through_build_gate(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
) -> None:
    """End-to-end (no monkeypatch): a real carve/hooks.toml post_build hook
    runs through the live BUILD-gated runner. `echo` is allow-listed at BUILD
    and exits 0 → the build succeeds and the Build stands."""
    config = _config(state_db=postgres_state_store_url)
    (project_dir / "carve").mkdir()
    (project_dir / "carve" / "hooks.toml").write_text(
        '[[hook]]\non = "post_build"\nrun = "echo built {pipeline_name} {build_id}"\n',
        encoding="utf-8",
    )
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_pb0005")

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses()),
    )
    assert artifact.success is True
    assert artifact.build_id is not None
    run = repository.get_run(artifact.run_id)
    assert run is not None and run.status == "success"


def test_m1_real_hooks_toml_denied_command_post_build_logs_build_stands(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
) -> None:
    """A real post_build hook whose command the BUILD gate DENIES ($()/;) is
    surfaced (the runner raises HookExecutionError) but, post-commit, the Build
    STANDS — the run stays success."""
    config = _config(state_db=postgres_state_store_url)
    (project_dir / "carve").mkdir()
    (project_dir / "carve" / "hooks.toml").write_text(
        '[[hook]]\non = "post_build"\nrun = "echo $(whoami)"\n',
        encoding="utf-8",
    )
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_pb0006")

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=_client_returning(*_success_responses()),
    )
    # The gate denied the dangerous command → the hook raised → but post-commit
    # the Build stands (the raise was logged, not propagated).
    assert artifact.success is True
    assert artifact.build_id is not None
    run = repository.get_run(artifact.run_id)
    assert run is not None and run.status == "success"


def test_m1_malformed_hooks_toml_keeps_build_fail_closed(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
) -> None:
    """A malformed carve/hooks.toml keeps the single-path build fail-closed:
    the HookConfigError propagates from the one config-parse boundary."""
    from carve.core.hooks.config import HookConfigError

    config = _config(state_db=postgres_state_store_url)
    plan = _plant_drafted_plan(repository, plan_id="plan_20260101_000000_pb0007")
    # First build a clean plan with no hooks file, so the plan is drafted.
    # (Plant directly — the plan is already drafted above.)
    (project_dir / "carve").mkdir()
    (project_dir / "carve" / "hooks.toml").write_text(
        '[[hook]]\non = "not_a_real_event"\nrun = "true"\n',
        encoding="utf-8",
    )
    with pytest.raises(HookConfigError):
        build_plan(
            plan_id=plan.id,
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),
        )


# ============================================================== multi-engine


def test_multi_engine_fires_post_build_after_recorded_build(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The multi-engine path fires post_build once, after the Build, with the
    authored union as `files` and the right payload."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id="plan_20260101_000000_pbme01")
    recording = _RecordingHook()
    _patch_hook(monkeypatch, recording)

    monkeypatch.setattr(
        builder_mod,
        "run_engines",
        lambda sub_goals, **kwargs: [
            _delegation("authored stripe", ["el/stripe/main.py"]),
            _delegation("authored staging", ["models/staging/stg_stripe.sql"]),
        ],
    )
    monkeypatch.setattr(builder_mod, "run_review_fan_out", lambda **kwargs: _clean_review())

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=object(),
    )

    assert artifact.success is True
    assert artifact.build_id is not None
    assert len(recording.payloads) == 1
    payload = recording.payloads[0]
    assert payload["pipeline_name"] == "stripe"
    assert payload["build_id"] == artifact.build_id
    assert payload["target"] == "dev"
    assert payload["plan_id"] == plan.id
    # `files` carries the authored UNION (same key name as the M1 path).
    assert payload["files"] == ["el/stripe/main.py", "models/staging/stg_stripe.sql"]


def test_multi_engine_failed_engine_does_not_fire_post_build(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed engine records NO Build → post_build never fires."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id="plan_20260101_000000_pbme02")
    recording = _RecordingHook()
    _patch_hook(monkeypatch, recording)

    monkeypatch.setattr(
        builder_mod,
        "run_engines",
        lambda sub_goals, **kwargs: [
            _delegation("authored stripe", ["el/stripe/main.py"]),
            _delegation("ran out of turns", [], status="failed"),
        ],
    )

    def _no_review(**_kwargs: Any) -> ReviewResult:
        raise AssertionError("review must not run when an engine failed")

    monkeypatch.setattr(builder_mod, "run_review_fan_out", _no_review)

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=object(),
    )
    assert artifact.success is False
    assert artifact.build_id is None
    assert recording.payloads == []


def test_multi_engine_review_blocked_does_not_fire_post_build(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review-blocked build records NO Build → post_build never fires."""
    from carve.core.agents.review_fan_out import Finding, Severity

    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id="plan_20260101_000000_pbme03")
    recording = _RecordingHook()
    _patch_hook(monkeypatch, recording)

    monkeypatch.setattr(
        builder_mod,
        "run_engines",
        lambda sub_goals, **kwargs: [
            _delegation("authored stripe", ["el/stripe/main.py"]),
            _delegation("authored staging", ["models/staging/stg_stripe.sql"]),
        ],
    )
    blocking = Finding(
        reviewer="python-reviewer",
        severity=Severity.BLOCKER,
        file="el/stripe/main.py",
        line=1,
        message="hardcoded secret",
    )
    monkeypatch.setattr(
        builder_mod,
        "run_review_fan_out",
        lambda **kwargs: ReviewResult(findings=[blocking], passed=False, by_reviewer={}, raw={}),
    )

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=object(),
    )
    assert artifact.success is False
    assert artifact.build_id is None
    assert recording.payloads == []


def test_multi_engine_raising_post_build_hook_build_stands(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-commit on the multi-engine path: a raising post_build hook is logged
    but the Build stands (run stays success, build_id present)."""
    config = _config(state_db=postgres_state_store_url)
    plan = _plant_multi_engine_plan(repository, plan_id="plan_20260101_000000_pbme04")
    _patch_hook(monkeypatch, _RecordingHook(raises=True))

    monkeypatch.setattr(
        builder_mod,
        "run_engines",
        lambda sub_goals, **kwargs: [
            _delegation("authored stripe", ["el/stripe/main.py"]),
            _delegation("authored staging", ["models/staging/stg_stripe.sql"]),
        ],
    )
    monkeypatch.setattr(builder_mod, "run_review_fan_out", lambda **kwargs: _clean_review())

    artifact = build_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=object(),
    )
    assert artifact.success is True
    assert artifact.build_id is not None
    run = repository.get_run(artifact.run_id)
    assert run is not None and run.status == "success"
    build = repository.get_build(artifact.build_id)
    assert build is not None
