"""Orchestrator tests for the unified recovery loop (P1-09).

The recovery agent itself is mocked — these tests focus on the loop's
sequencing: do-not-fix gating, child-Run linkage via ``parent_run_id``,
budget exhaustion, repeated-identical detection, Ctrl-C handling, and
the ``--no-auto-fix`` flag short-circuit.

The pattern mirrors P1-08's ``_FakeRecoveryHandler``: the test injects
canned :class:`RecoveryAttemptResult` instances per attempt and asserts
on the resulting outcome / persisted state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from carve.cli.orchestrator import recovery as recovery_mod
from carve.cli.orchestrator.recovery import (
    Aborted,
    ExecutionResult,
    Exhausted,
    Recovered,
    Refused,
    run_with_recovery,
)
from carve.core.agents.recovery import (
    DeployDdlApplyInvocation,
    DeployPreflightInvocation,
    DeployVerifyInvocation,
    ElRunInvocation,
    Invocation,
    RecoveryAttemptResult,
)
from carve.core.config.schema import (
    Config,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(project_dir: Path) -> Config:
    return Config(
        project=ProjectConfig(name="rec-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(state_store=f"sqlite:///{project_dir}/.carve/state.db"),
    )


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    (tmp_path / ".carve").mkdir(parents=True, exist_ok=True)
    config = _make_config(tmp_path)
    engine = create_engine_from_config(config, project_dir=tmp_path)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def el_invocation(project_dir: Path) -> ElRunInvocation:
    return ElRunInvocation(
        pipeline_name="iowa",
        active_target="dev",
        project_dir=project_dir,
        config=_make_config(project_dir),
        failed_run_id="",
        error_text="",
    )


# ---------------------------------------------------------------------------
# Test harness helpers
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedAgent:
    """Replaces `_run_one_attempt` to script the agent's responses.

    Each call pops the next canned result. The orchestrator's loop is
    what's actually under test; the agent is treated as a black box.
    """

    results: list[RecoveryAttemptResult]
    calls: int = 0

    def __call__(self, **kwargs: object) -> RecoveryAttemptResult:
        del kwargs
        self.calls += 1
        if not self.results:
            raise AssertionError(
                f"agent script exhausted at call {self.calls}; "
                "did the loop call more times than expected?"
            )
        return self.results.pop(0)


def _patch_agent(
    monkeypatch: pytest.MonkeyPatch,
    results: list[RecoveryAttemptResult],
) -> _ScriptedAgent:
    agent = _ScriptedAgent(results=list(results))
    monkeypatch.setattr(recovery_mod, "_run_one_attempt", agent)
    return agent


def _execute_factory(
    repository: Repository,
    *,
    pipeline_name: str = "iowa",
    target: str = "dev",
    outcomes: list[ExecutionResult] | None = None,
    successes_after: int | None = None,
    error: str = "boom",
    distinct_errors: bool = False,
) -> Callable[[str | None], ExecutionResult]:
    """Build an `execute` callable that creates a real Run row per call.

    Two driving modes:

    * ``outcomes`` — explicit per-call ExecutionResults. The factory
      still calls `repository.create_run` to wire up the `parent_run_id`.
    * ``successes_after`` — fail until the Nth call (1-indexed), then
      succeed. ``None`` means always fail.

    ``distinct_errors=True`` appends the call count to ``error`` so the
    loop-detection guard doesn't trip — used by tests that exercise
    multiple retries without wanting to mimic a "fix didn't take" loop.
    """
    state = {"call_count": 0}
    queue = list(outcomes) if outcomes else None

    def _execute(parent_run_id: str | None) -> ExecutionResult:
        state["call_count"] += 1
        run_id = repository.create_run(
            kind="run",
            target_id=pipeline_name,
            pipeline_name=pipeline_name,
            target=target,
            parent_run_id=parent_run_id,
        )
        if queue is not None:
            spec = queue.pop(0)
            # Override the run_id so the test sees the real Run row id.
            return ExecutionResult(
                run_id=run_id,
                success=spec.success,
                error=spec.error,
            )
        if successes_after is not None and state["call_count"] >= successes_after:
            repository.update_run_status(run_id, "success")
            return ExecutionResult(run_id=run_id, success=True, error="")
        err = (
            f"{error} (#{state['call_count']})" if distinct_errors else error
        )
        repository.update_run_status(run_id, "failed", error=err)
        return ExecutionResult(run_id=run_id, success=False, error=err)

    return _execute


# ---------------------------------------------------------------------------
# Per-context recovery happy paths
# ---------------------------------------------------------------------------


def test_recovery_run_failure_recovered(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    el_invocation: ElRunInvocation,
) -> None:
    """el run failure → agent fixes → retry succeeds → 1 child run."""
    _patch_agent(
        monkeypatch,
        [
            RecoveryAttemptResult(
                category="code_fix",
                summary="json.dumps the dict",
                action_taken="edited main.py",
                refused=False,
            )
        ],
    )
    execute = _execute_factory(
        repository,
        successes_after=2,  # first call fails, second succeeds
        error="dict not JSON serializable",
    )

    outcome = run_with_recovery(
        el_invocation,
        execute=execute,
        repository=repository,
        max_attempts=3,
    )
    assert isinstance(outcome, Recovered)
    assert outcome.attempts == 1

    # Two runs: original (failed) and one child.
    runs = repository.list_runs(pipeline_name="iowa")
    assert len(runs) == 2
    parent = next(r for r in runs if r.parent_run_id is None)
    child = next(r for r in runs if r.parent_run_id == parent.id)
    assert child.parent_run_id == parent.id


def test_recovery_deploy_phase1_drift_recovered(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    project_dir: Path,
) -> None:
    """Phase 1 drift detected → agent edits DDL → retry succeeds."""
    invocation = DeployPreflightInvocation(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        project_dir=project_dir,
        config=_make_config(project_dir),
        failed_run_id="",
        error_text="",
        ddl_path=project_dir / "el/iowa/snowflake.sql",
        drift=("STORE column type mismatch",),
    )
    _patch_agent(
        monkeypatch,
        [
            RecoveryAttemptResult(
                category="code_fix",
                summary="adjusted column type",
                action_taken="edited DDL",
                refused=False,
            )
        ],
    )
    execute = _execute_factory(
        repository, successes_after=2, error="STORE column type mismatch"
    )
    outcome = run_with_recovery(
        invocation, execute=execute, repository=repository, max_attempts=3
    )
    assert isinstance(outcome, Recovered)
    assert outcome.attempts == 1


def test_recovery_deploy_phase2_ddl_failure_recovered(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    project_dir: Path,
) -> None:
    """DDL stmt failure → agent fixes → retry succeeds."""
    invocation = DeployDdlApplyInvocation(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        project_dir=project_dir,
        config=_make_config(project_dir),
        failed_run_id="",
        error_text="",
        ddl_path=project_dir / "el/iowa/snowflake.sql",
        failing_statement_index=2,
        failing_sql="GRANT INSERT ON TABLE iowa TO ROLE r;",
    )
    _patch_agent(
        monkeypatch,
        [
            RecoveryAttemptResult(
                category="code_fix",
                summary="reordered DDL",
                action_taken="moved GRANT after CREATE",
                refused=False,
            )
        ],
    )
    execute = _execute_factory(
        repository, successes_after=2, error="object does not exist"
    )
    outcome = run_with_recovery(
        invocation, execute=execute, repository=repository, max_attempts=3
    )
    assert isinstance(outcome, Recovered)
    assert outcome.attempts == 1


def test_recovery_deploy_phase3_verify_failure_recovered(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    project_dir: Path,
) -> None:
    """Verify failure → agent appends GRANT → retry succeeds."""
    invocation = DeployVerifyInvocation(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        project_dir=project_dir,
        config=_make_config(project_dir),
        failed_run_id="",
        error_text="",
        ddl_path=project_dir / "el/iowa/snowflake.sql",
    )
    _patch_agent(
        monkeypatch,
        [
            RecoveryAttemptResult(
                category="code_fix",
                summary="appended grants",
                action_taken="added GRANT INSERT",
                refused=False,
            )
        ],
    )
    execute = _execute_factory(
        repository, successes_after=2, error="missing INSERT grant"
    )
    outcome = run_with_recovery(
        invocation, execute=execute, repository=repository, max_attempts=3
    )
    assert isinstance(outcome, Recovered)
    assert outcome.attempts == 1


# ---------------------------------------------------------------------------
# Failure-mode coverage
# ---------------------------------------------------------------------------


def test_recovery_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    el_invocation: ElRunInvocation,
) -> None:
    """Three attempts, all fail → Exhausted with last diagnosis."""
    _patch_agent(
        monkeypatch,
        [
            RecoveryAttemptResult(
                category="code_fix",
                summary=f"attempt {n}",
                action_taken=f"edit {n}",
                refused=False,
            )
            for n in (1, 2, 3)
        ],
    )
    # Always fail; bake the error per call so the loop-detect doesn't
    # trip on identical diagnoses (we want the budget-exhausted path).
    state = {"n": 0}

    def execute(parent_run_id: str | None) -> ExecutionResult:
        state["n"] += 1
        run_id = repository.create_run(
            kind="run",
            target_id="iowa",
            pipeline_name="iowa",
            target="dev",
            parent_run_id=parent_run_id,
        )
        repository.update_run_status(
            run_id, "failed", error=f"failure-{state['n']}"
        )
        return ExecutionResult(run_id=run_id, success=False, error=f"failure-{state['n']}")

    outcome = run_with_recovery(
        el_invocation,
        execute=execute,
        repository=repository,
        max_attempts=3,
    )
    assert isinstance(outcome, Exhausted)
    assert outcome.attempts == 3
    assert "attempt 3" in outcome.diagnosis


def test_recovery_refuses_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    el_invocation: ElRunInvocation,
) -> None:
    """Auth failure → Refused, no LLM call."""
    agent = _patch_agent(monkeypatch, [])
    execute = _execute_factory(
        repository, error="Authentication failed: bad password"
    )
    outcome = run_with_recovery(
        el_invocation,
        execute=execute,
        repository=repository,
        max_attempts=3,
    )
    assert isinstance(outcome, Refused)
    assert outcome.category == "auth"
    assert agent.calls == 0


def test_recovery_refuses_repeated_identical_failure(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    el_invocation: ElRunInvocation,
) -> None:
    """Two attempts produce identical errors → Exhausted via loop-guard."""
    _patch_agent(
        monkeypatch,
        [
            RecoveryAttemptResult(
                category="code_fix",
                summary="will try X",
                action_taken="edit",
                refused=False,
            ),
            RecoveryAttemptResult(
                category="code_fix",
                summary="will try X",  # identical diagnosis
                action_taken="edit",
                refused=False,
            ),
        ],
    )
    # Same error each time triggers the post-retry guard.
    execute = _execute_factory(
        repository, error="dict not JSON serializable"
    )
    outcome = run_with_recovery(
        el_invocation,
        execute=execute,
        repository=repository,
        max_attempts=4,
    )
    assert isinstance(outcome, Exhausted)
    assert outcome.last_category == "repeated_identical"


def test_recovery_aborted_on_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    el_invocation: ElRunInvocation,
) -> None:
    """KeyboardInterrupt mid-recovery → Aborted with attempts-so-far."""

    def raising_agent(**kwargs: object) -> RecoveryAttemptResult:
        del kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(recovery_mod, "_run_one_attempt", raising_agent)
    execute = _execute_factory(repository, error="boom")

    outcome = run_with_recovery(
        el_invocation,
        execute=execute,
        repository=repository,
        max_attempts=3,
    )
    assert isinstance(outcome, Aborted)
    # No orphaned recovery state — the agent never finished a single
    # attempt, so the failed parent run is the only row.
    runs = repository.list_runs(pipeline_name="iowa")
    assert len(runs) == 1


def test_recovery_no_auto_fix_flag(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    el_invocation: ElRunInvocation,
) -> None:
    """`auto_fix=False` short-circuits — no agent call, no children."""
    agent = _patch_agent(monkeypatch, [])
    execute = _execute_factory(repository, error="boom")
    outcome = run_with_recovery(
        el_invocation,
        execute=execute,
        repository=repository,
        max_attempts=3,
        auto_fix=False,
    )
    assert isinstance(outcome, Refused)
    assert agent.calls == 0
    runs = repository.list_runs(pipeline_name="iowa")
    assert len(runs) == 1


def test_recovery_chain_persisted_via_parent_run_id(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    el_invocation: ElRunInvocation,
) -> None:
    """Two failed attempts then success → 3 runs in the chain."""
    _patch_agent(
        monkeypatch,
        [
            RecoveryAttemptResult(
                category="code_fix",
                summary="try A",
                action_taken="A",
                refused=False,
            ),
            RecoveryAttemptResult(
                category="code_fix",
                summary="try B",
                action_taken="B",
                refused=False,
            ),
        ],
    )
    state = {"n": 0}

    def execute(parent_run_id: str | None) -> ExecutionResult:
        state["n"] += 1
        run_id = repository.create_run(
            kind="run",
            target_id="iowa",
            pipeline_name="iowa",
            target="dev",
            parent_run_id=parent_run_id,
        )
        if state["n"] >= 3:
            repository.update_run_status(run_id, "success")
            return ExecutionResult(run_id=run_id, success=True, error="")
        err = f"failure-{state['n']}"
        repository.update_run_status(run_id, "failed", error=err)
        return ExecutionResult(run_id=run_id, success=False, error=err)

    outcome = run_with_recovery(
        el_invocation,
        execute=execute,
        repository=repository,
        max_attempts=3,
    )
    assert isinstance(outcome, Recovered)
    assert outcome.attempts == 2

    runs = repository.list_runs(pipeline_name="iowa")
    assert len(runs) == 3
    parent = next(r for r in runs if r.parent_run_id is None)
    children = repository.get_recovery_children(parent.id)
    # Children form a chain (child of child); only one is direct.
    assert len(children) == 1
    grandchildren = repository.get_recovery_children(children[0].id)
    assert len(grandchildren) == 1


# ---------------------------------------------------------------------------
# Per-context budget independence
# ---------------------------------------------------------------------------


def test_recovery_per_context_budgets_independent(
    monkeypatch: pytest.MonkeyPatch,
    repository: Repository,
    project_dir: Path,
) -> None:
    """Three deploy phases each get their own budget pool.

    Single deploy with drift (1 attempt to recover) + DDL fail (2
    attempts) + verify fail (1 attempt) — each call to
    `run_with_recovery` carries its own attempt count; the orchestrator
    is the layer that drives them sequentially.
    """
    config = _make_config(project_dir)
    ddl_path = project_dir / "el/iowa/snowflake.sql"
    phase_invocations: list[Invocation] = [
        DeployPreflightInvocation(
            pipeline_name="iowa",
            source_target="dev",
            dest_target="prod",
            project_dir=project_dir,
            config=config,
            failed_run_id="",
            error_text="",
            ddl_path=ddl_path,
            drift=(),
        ),
        DeployDdlApplyInvocation(
            pipeline_name="iowa",
            source_target="dev",
            dest_target="prod",
            project_dir=project_dir,
            config=config,
            failed_run_id="",
            error_text="",
            ddl_path=ddl_path,
        ),
        DeployVerifyInvocation(
            pipeline_name="iowa",
            source_target="dev",
            dest_target="prod",
            project_dir=project_dir,
            config=config,
            failed_run_id="",
            error_text="",
            ddl_path=ddl_path,
        ),
    ]
    # Drift recovers in 1, DDL recovers in 2, verify recovers in 1.
    phase_attempts_to_success = [2, 3, 2]
    phase_agent_results = [
        [_canned("first")],
        [_canned("first"), _canned("second")],
        [_canned("first")],
    ]

    for invocation, success_at, agents in zip(
        phase_invocations, phase_attempts_to_success, phase_agent_results, strict=True
    ):
        _patch_agent(monkeypatch, agents)
        execute = _execute_factory(
            repository,
            pipeline_name=f"iowa-{invocation.trigger.value}",
            successes_after=success_at,
            error="phase failure",
            distinct_errors=True,
        )
        outcome = run_with_recovery(
            invocation,
            execute=execute,
            repository=repository,
            max_attempts=3,
        )
        assert isinstance(outcome, Recovered)
        assert outcome.attempts == success_at - 1


def _canned(label: str) -> RecoveryAttemptResult:
    return RecoveryAttemptResult(
        category="code_fix",
        summary=f"diagnosis-{label}",
        action_taken=f"action-{label}",
        refused=False,
    )


# ---------------------------------------------------------------------------
# LLMRecoveryHandler — connection-role per stage
# ---------------------------------------------------------------------------


class TestLLMRecoveryHandlerConnectionRoles:
    """Iter1's MF3 wired ONE connection (deploy role) for all stages.
    Iter2 splits deploy_query_runner / runtime_query_runner so:
      - PREFLIGHT and DDL_APPLY use deploy-role query runner.
      - VERIFY uses runtime-role query runner (matches what verify itself
        uses; recovery doesn't elevate privileges to inspect state).
    These tests pin the contract by asserting which runner is forwarded
    into ``run_recovery_agent`` per stage.
    """

    def test_verify_stage_uses_runtime_query_runner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from carve.core.agents.recovery import LLMRecoveryHandler
        from carve.core.deploy.recovery import RecoveryContext, RecoveryStage

        deploy_runner = object()
        runtime_runner = object()
        recorded: dict[str, object] = {}

        def fake_run_recovery_agent(
            invocation: object,
            **kwargs: object,
        ) -> RecoveryAttemptResult:
            recorded["query_runner"] = kwargs.get("snowflake_query_runner")
            return _canned("verify")

        monkeypatch.setattr(
            "carve.core.agents.recovery.agent.run_recovery_agent",
            fake_run_recovery_agent,
        )

        # Build a minimal Config + Repository so config-loading inside the
        # handler doesn't try to read disk.
        project_dir = tmp_path / "p"
        (project_dir / "carve").mkdir(parents=True)
        (project_dir / ".carve").mkdir(parents=True)
        (project_dir / "carve.toml").write_text(
            '[project]\nname = "p"\ndefault_target = "dev"\n'
        )
        (project_dir / "carve" / "connections.toml").write_text(
            '[snowflake.dev]\naccount = "a"\nuser = "u"\nrole = "R"\n'
            'warehouse = "W"\ndatabase = "D"\n'
        )
        config = Config(
            project=ProjectConfig(name="p"),
            models=ModelsConfig(anthropic_api_key="sk"),
            server=ServerConfig(state_store=f"sqlite:///{project_dir}/.carve/state.db"),
        )
        engine = create_engine_from_config(config, project_dir=project_dir)
        initialize_database(engine)
        repository = Repository(create_session_factory(engine))

        handler = LLMRecoveryHandler(
            config=config,
            repository=repository,
            deploy_query_runner=deploy_runner,
            runtime_query_runner=runtime_runner,
        )
        ctx = RecoveryContext(
            stage=RecoveryStage.VERIFY,
            pipeline_name="iowa",
            source_target="dev",
            dest_target="dev",
            project_dir=project_dir,
            error="verify failed",
            ddl_path=project_dir / "el/iowa/snowflake.sql",
        )
        handler.attempt(ctx)
        assert recorded["query_runner"] is runtime_runner

    def test_ddl_apply_stage_uses_deploy_query_runner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from carve.core.agents.recovery import LLMRecoveryHandler
        from carve.core.deploy.recovery import RecoveryContext, RecoveryStage

        deploy_runner = object()
        runtime_runner = object()
        recorded: dict[str, object] = {}

        def fake_run_recovery_agent(
            invocation: object,
            **kwargs: object,
        ) -> RecoveryAttemptResult:
            recorded["query_runner"] = kwargs.get("snowflake_query_runner")
            return _canned("ddl")

        monkeypatch.setattr(
            "carve.core.agents.recovery.agent.run_recovery_agent",
            fake_run_recovery_agent,
        )

        project_dir = tmp_path / "p2"
        (project_dir / "carve").mkdir(parents=True)
        (project_dir / ".carve").mkdir(parents=True)
        (project_dir / "carve.toml").write_text(
            '[project]\nname = "p"\ndefault_target = "dev"\n'
        )
        (project_dir / "carve" / "connections.toml").write_text(
            '[snowflake.dev]\naccount = "a"\nuser = "u"\nrole = "R"\n'
            'warehouse = "W"\ndatabase = "D"\n'
        )
        config = Config(
            project=ProjectConfig(name="p"),
            models=ModelsConfig(anthropic_api_key="sk"),
            server=ServerConfig(state_store=f"sqlite:///{project_dir}/.carve/state.db"),
        )
        engine = create_engine_from_config(config, project_dir=project_dir)
        initialize_database(engine)
        repository = Repository(create_session_factory(engine))

        handler = LLMRecoveryHandler(
            config=config,
            repository=repository,
            deploy_query_runner=deploy_runner,
            runtime_query_runner=runtime_runner,
        )
        ctx = RecoveryContext(
            stage=RecoveryStage.DDL_APPLY,
            pipeline_name="iowa",
            source_target="dev",
            dest_target="dev",
            project_dir=project_dir,
            error="ddl failed",
            ddl_path=project_dir / "el/iowa/snowflake.sql",
            failing_statement_index=2,
            failing_sql="CREATE TABLE IF NOT EXISTS X (A INT);",
        )
        handler.attempt(ctx)
        assert recorded["query_runner"] is deploy_runner
