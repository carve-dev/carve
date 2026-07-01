"""Recording: a delegated subagent run persists its invocation + skill calls.

Postgres-fixture-gated (the agent-telemetry tables live in Postgres). Two axes:

* The migration ``0012`` creates all three tables (``agents``/``agent_invocations``/
  ``skill_calls``) with their access indexes. All four external correlation ids
  (``run_id``/``plan_id``/``build_id``/``ask_id``) are nullable NO-FK recording
  pointers — the plan/build paths record telemetry *before* the parent row exists
  — so ``agent_name`` is the only enforced FK on ``agent_invocations``.
* The :class:`RecordingObserver` wired at the delegation call-site
  (``_delegate_engine``) writes one ``agent_invocations`` row (tokens/cost/
  duration/status) correlated to the run/plan + one ``skill_calls`` row per tool
  call — best-effort, never blocking the delegated run.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

from carve.cli.orchestrator import delegation_run
from carve.cli.orchestrator.goal_decomposer import SubGoal
from carve.core.agents.delegation import DelegationResult
from carve.core.agents.loop import TokenUsage
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.observability.recording import RecordingObserver
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Agent, AgentInvocation, Plan, Run, SkillCall
from carve.core.state.telemetry import TelemetryRepo


def _make_config(url: str) -> Config:
    return Config(
        project=ProjectConfig(name="observability-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=url),
    )


def _prepare_db(url: str):  # type: ignore[no-untyped-def]
    engine = create_engine_from_config(_make_config(url))
    initialize_database(engine)
    return create_session_factory(engine), engine


def _seed_run_and_plan(session_factory) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    run_id = "run_" + uuid.uuid4().hex
    plan_id = "plan_" + uuid.uuid4().hex
    with session_factory() as session:
        session.add(
            Plan(
                id=plan_id,
                goal="load stripe",
                config_hash="h",
                carve_version="0.0.1",
                task_graph_json={},
                file_path="/tmp/p.json",
            )
        )
        session.add(Run(id=run_id, kind="plan", target_id=plan_id, status="running"))
        session.commit()
    return run_id, plan_id


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_creates_agent_telemetry_tables(postgres_state_store_url: str) -> None:
    """0012 lands agents/agent_invocations/skill_calls with indexes + the right FKs."""
    engine = create_engine_from_config(_make_config(postgres_state_store_url))
    try:
        initialize_database(engine)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"agents", "agent_invocations", "skill_calls"}.issubset(tables)

        assert "ix_agent_invocations_agent_name_started_at" in {
            ix["name"] for ix in inspector.get_indexes("agent_invocations")
        }
        assert "ix_agent_invocations_run_id" in {
            ix["name"] for ix in inspector.get_indexes("agent_invocations")
        }
        assert "ix_skill_calls_agent_invocation_id" in {
            ix["name"] for ix in inspector.get_indexes("skill_calls")
        }

        # All four external correlation ids (run_id/plan_id/build_id/ask_id) are
        # nullable NO-FK recording pointers; ``agent_name`` is the ONLY enforced
        # FK on this table. Pin the full set so an accidental FK re-add fails loud.
        constrained = {
            tuple(fk["constrained_columns"])
            for fk in inspector.get_foreign_keys("agent_invocations")
        }
        assert constrained == {("agent_name",)}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Record-before-parent ordering (the plan-path drop regression)
# ---------------------------------------------------------------------------


def test_open_invocation_before_parent_rows_exist_still_persists(
    postgres_state_store_url: str,
) -> None:
    """Recording a ``plan_id``/``build_id`` whose parent row does NOT exist yet
    still persists the invocation + its skill calls (no-FK recording pointers).

    Regression for the silently-dropped plan-path telemetry: ``generate_plan``
    mints ``plan_id`` and runs the engineers *with recording live* BEFORE it
    inserts the ``plans`` row. Under the old ``plan_id → plans.id`` FK,
    ``open_invocation`` raised ``IntegrityError`` at commit, ``begin_invocation``
    swallowed it, and the whole invocation + every skill call vanished — so
    ``carve plan`` recorded zero telemetry. With the FK demoted this ordering
    must persist by value. (The ``build_id`` half covers the analogous
    ``builds``-row-not-yet-written case.)
    """
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        telemetry = TelemetryRepo(session_factory)
        # Ids with NO corresponding plans/builds rows — the exact fresh-Postgres
        # scenario that failed under the FK.
        plan_id = "plan_" + uuid.uuid4().hex
        build_id = "build_" + uuid.uuid4().hex
        inv_id = telemetry.open_invocation(
            agent_name="dlt-engineer", plan_id=plan_id, build_id=build_id
        )
        telemetry.record_skill_call(agent_invocation_id=inv_id, skill_name="edit_file")

        with session_factory() as session:
            inv = session.get(AgentInvocation, inv_id)
            assert inv is not None
            assert inv.plan_id == plan_id
            assert inv.build_id == build_id
            skills = session.scalars(
                sa.select(SkillCall).where(SkillCall.agent_invocation_id == inv_id)
            ).all()
            assert [sk.skill_name for sk in skills] == ["edit_file"]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Recording at the call-site
# ---------------------------------------------------------------------------


def test_delegate_engine_records_invocation_and_skill_calls(
    postgres_state_store_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delegated run writes one invocation (tokens/cost/duration/status) + a
    skill_calls row per tool call, correlated to the run/plan; ask_id is NULL."""
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        run_id, plan_id = _seed_run_and_plan(session_factory)
        observer = RecordingObserver(TelemetryRepo(session_factory), run_id=run_id, plan_id=plan_id)
        result = DelegationResult(
            status="succeeded",
            result_summary="ok",
            files_changed=[],
            outputs={},
            usage=TokenUsage(input_tokens=1200, output_tokens=340),
            cost_usd=0.0123,
        )

        class _FakeRunner:
            """Simulates the loop firing two tool results, then returning."""

            def run(self, agent, task, context, *, parent_mode, depth=1):  # type: ignore[no-untyped-def]
                observer.on_tool_result("edit_file", True, "wrote 12 lines", 8)
                observer.on_tool_result("bash", True, "exit 0", 15)
                return result

        monkeypatch.setattr(
            delegation_run,
            "select_agent",
            lambda registry, *, classification: "dlt-engineer",
        )

        out = delegation_run._delegate_engine(
            SubGoal(sub_goal="load stripe", classification="dlt"),
            registry=object(),
            runner=_FakeRunner(),
            parent_mode=PermissionMode.PLAN,
            observer=observer,
        )
        assert out is result

        with session_factory() as session:
            invocations = list(session.scalars(sa.select(AgentInvocation)).all())
            assert len(invocations) == 1
            inv = invocations[0]
            assert inv.agent_name == "dlt-engineer"
            assert inv.run_id == run_id
            assert inv.plan_id == plan_id
            assert inv.build_id is None
            assert inv.ask_id is None  # nullable no-FK column until ask ships
            assert inv.tokens_input == 1200
            assert inv.tokens_output == 340
            assert inv.cost_usd == pytest.approx(0.0123)
            assert inv.status == "succeeded"
            assert inv.duration_ms is not None and inv.duration_ms >= 0

            skills = list(
                session.scalars(
                    sa.select(SkillCall).where(SkillCall.agent_invocation_id == inv.id)
                ).all()
            )
            assert len(skills) == 2
            assert {sk.skill_name for sk in skills} == {"edit_file", "bash"}

            # The FK-parent agent row was upserted.
            assert session.get(Agent, "dlt-engineer") is not None
    finally:
        engine.dispose()


def test_delegate_engine_finalizes_failed_when_delegate_raises(
    postgres_state_store_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the delegate call raises, the opened invocation is finalized ``failed``
    and the original error still propagates (recording never masks a failure)."""
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        run_id, _ = _seed_run_and_plan(session_factory)
        observer = RecordingObserver(TelemetryRepo(session_factory), run_id=run_id)

        class _BoomRunner:
            def run(self, agent, task, context, *, parent_mode, depth=1):  # type: ignore[no-untyped-def]
                raise RuntimeError("boom")

        monkeypatch.setattr(
            delegation_run,
            "select_agent",
            lambda registry, *, classification: "dbt-engineer",
        )

        with pytest.raises(RuntimeError, match="boom"):
            delegation_run._delegate_engine(
                SubGoal(sub_goal="model it", classification="dbt"),
                registry=object(),
                runner=_BoomRunner(),
                parent_mode=PermissionMode.BUILD,
                observer=observer,
            )

        with session_factory() as session:
            inv = session.scalars(sa.select(AgentInvocation)).one()
            assert inv.status == "failed"
            assert inv.duration_ms is not None
    finally:
        engine.dispose()


def test_on_tool_result_without_open_invocation_is_dropped(
    postgres_state_store_url: str,
) -> None:
    """A stray ``on_tool_result`` with no open invocation writes nothing (no crash)."""
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        observer = RecordingObserver(TelemetryRepo(session_factory))
        # No begin_invocation — the cursor is None.
        observer.on_tool_result("bash", True, "noop", 3)
        with session_factory() as session:
            assert session.scalars(sa.select(SkillCall)).all() == []
    finally:
        engine.dispose()


def test_failed_tool_call_still_records_skill_call_with_summary_length(
    postgres_state_store_url: str,
) -> None:
    """A failed (``ok=False``) tool call still lands a ``skill_calls`` row; the
    ``output_size`` proxy is the error-summary length (pins that branch)."""
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        run_id, _ = _seed_run_and_plan(session_factory)
        observer = RecordingObserver(TelemetryRepo(session_factory), run_id=run_id)
        inv_id = observer.begin_invocation(agent_name="dlt-engineer")
        assert inv_id is not None

        err = "Traceback: connection refused to warehouse"
        observer.on_tool_result("run_sql", False, err, 42)

        with session_factory() as session:
            call = session.scalars(
                sa.select(SkillCall).where(SkillCall.agent_invocation_id == inv_id)
            ).one()
            assert call.skill_name == "run_sql"
            # ``output_size`` is the summary length even on the failed branch.
            assert call.output_size == len(err)
            assert call.duration_ms == 42
    finally:
        engine.dispose()


def test_begin_invocation_returns_id_and_warns_on_double_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``begin_invocation`` returns the minted id; ``end_invocation`` finalizes the
    id **passed to it** (not the mutable cursor); a second open before a close
    fails loud (warns), never a silent overwrite.

    Pure unit (a fake ``TelemetryRepo``) — no DB, so Alembic's ``fileConfig``
    never wipes pytest's log-capture handler and the warning is observable.
    """

    class _FakeRepo:
        def __init__(self) -> None:
            self.finalized: list[tuple[str, str, int]] = []
            self._n = 0

        def open_invocation(self, **_kwargs: object) -> str:
            self._n += 1
            return f"inv_{self._n}"

        def finalize_invocation(
            self, invocation_id: str, *, status: str, duration_ms: int, **_kwargs: object
        ) -> None:
            self.finalized.append((invocation_id, status, duration_ms))

    # A migration-running test earlier in the session may have disabled this
    # logger process-wide (Alembic ``fileConfig(disable_existing_loggers=True)``);
    # re-enable it so the fail-loud warning is observable regardless of order.
    logging.getLogger("carve.core.observability.recording").disabled = False

    repo = _FakeRepo()
    observer = RecordingObserver(repo)  # type: ignore[arg-type]

    first = observer.begin_invocation(agent_name="dlt-engineer")
    assert first == "inv_1"

    # A second open before closing the first must warn (latent orphan signal).
    with caplog.at_level(logging.WARNING, logger="carve.core.observability.recording"):
        second = observer.begin_invocation(agent_name="dbt-engineer")
    assert second == "inv_2"
    assert any("already-open invocation" in rec.getMessage() for rec in caplog.records)

    # Finalize by the returned id ("inv_1"), even though the cursor now points at
    # "inv_2" — proving the finalize no longer depends on the mutable cursor.
    result = DelegationResult(
        status="succeeded",
        result_summary="ok",
        files_changed=[],
        outputs={},
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        cost_usd=0.001,
    )
    observer.end_invocation(first, result, 7)
    assert ("inv_1", "succeeded", 7) in repo.finalized


def test_run_engines_records_invocation_at_production_seam(
    postgres_state_store_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The production ``run_engines`` seam wires a RecordingObserver from a real
    ``session_factory``: a delegated engineer lands an ``agent_invocations`` row
    (+ its skill call) correlated to the run/plan.

    This is the per-call-site assertion the reviewer said "would have caught" the
    dormant-recording bug — it drives the same entry point ``builder.py`` /
    ``planner.py`` call (``run_engines(..., session_factory=<real>, run_id=<seeded>,
    plan_id=<seeded>)``) with an injected fake runner and reads the row back.
    """
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        run_id, plan_id = _seed_run_and_plan(session_factory)
        result = DelegationResult(
            status="succeeded",
            result_summary="ok",
            files_changed=[],
            outputs={},
            usage=TokenUsage(input_tokens=800, output_tokens=120),
            cost_usd=0.004,
        )

        # Capture the observer the seam builds so the fake runner can fire a tool
        # result through it (the real runner would receive it via _build_runner).
        captured: dict[str, RecordingObserver | None] = {}
        real_make = delegation_run._make_recording_observer

        def _capturing_make(sf, **kwargs):  # type: ignore[no-untyped-def]
            observer = real_make(sf, **kwargs)
            captured["observer"] = observer
            return observer

        monkeypatch.setattr(delegation_run, "_make_recording_observer", _capturing_make)
        monkeypatch.setattr(
            delegation_run,
            "select_agent",
            lambda registry, *, classification: "dlt-engineer",
        )

        class _FakeRunner:
            def run(self, agent, task, context, *, parent_mode, depth=1):  # type: ignore[no-untyped-def]
                observer = captured["observer"]
                assert observer is not None
                observer.on_tool_result("bash", True, "exit 0", 9)
                return result

        results = delegation_run.run_engines(
            [SubGoal(sub_goal="load stripe", classification="dlt")],
            config=_make_config(postgres_state_store_url),
            project_dir=tmp_path,
            client=object(),
            model="claude-sonnet-4-6",
            registry=object(),
            runner=_FakeRunner(),
            parent_mode=PermissionMode.BUILD,
            session_factory=session_factory,
            run_id=run_id,
            plan_id=plan_id,
        )
        assert results == [result]

        with session_factory() as session:
            inv = session.scalars(sa.select(AgentInvocation)).one()
            assert inv.run_id == run_id
            assert inv.plan_id == plan_id
            assert inv.build_id is None
            assert inv.agent_name == "dlt-engineer"
            assert inv.status == "succeeded"
            assert inv.tokens_input == 800
            skills = session.scalars(
                sa.select(SkillCall).where(SkillCall.agent_invocation_id == inv.id)
            ).all()
            assert [sk.skill_name for sk in skills] == ["bash"]
    finally:
        engine.dispose()
