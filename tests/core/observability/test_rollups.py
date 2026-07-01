"""Rollups: ``carve metrics`` costs / runs / agents aggregate correctly.

Postgres-fixture-gated. Seeds ``agents``/``agent_invocations``/``skill_calls`` +
``runs`` directly, then asserts :class:`MetricsRollups` computes token→USD sums,
run success/failure + median/p95 duration + by-target breakdown, and per-agent
usage (+ skill-call mix + success rate). Also covers the ``--since`` window
parser and its filtering.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carve.core.agents.pricing import compute_cost_usd
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.observability.rollups import MetricsRollups, parse_since
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Agent, AgentInvocation, Pipeline, Run, SkillCall

_MODEL = "claude-sonnet-4-6"
_C1 = compute_cost_usd(_MODEL, input_tokens=1000, output_tokens=200)
_C2 = compute_cost_usd(_MODEL, input_tokens=500, output_tokens=100)
_C3 = compute_cost_usd(_MODEL, input_tokens=2000, output_tokens=400)


def _make_config(url: str) -> Config:
    return Config(
        project=ProjectConfig(name="rollups-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=url),
    )


def _prepare_db(url: str):  # type: ignore[no-untyped-def]
    engine = create_engine_from_config(_make_config(url))
    initialize_database(engine)
    return create_session_factory(engine), engine


def _seed(session_factory) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    with session_factory() as session:
        # ``runs.pipeline_name`` FKs to ``pipelines.name`` — seed the parents.
        session.add_all(
            [
                Pipeline(name="sales", pipeline_dir="el/sales"),
                Pipeline(name="orders", pipeline_dir="el/orders"),
                Agent(name="dlt-engineer"),
                Agent(name="dbt-engineer"),
            ]
        )
        session.flush()
        session.add_all(
            [
                AgentInvocation(
                    id="inv1",
                    agent_name="dlt-engineer",
                    tokens_input=1000,
                    tokens_output=200,
                    cost_usd=_C1,
                    duration_ms=1200,
                    status="succeeded",
                    started_at=now,
                ),
                AgentInvocation(
                    id="inv2",
                    agent_name="dlt-engineer",
                    tokens_input=500,
                    tokens_output=100,
                    cost_usd=_C2,
                    duration_ms=800,
                    status="failed",
                    started_at=now,
                ),
                AgentInvocation(
                    id="inv3",
                    agent_name="dbt-engineer",
                    tokens_input=2000,
                    tokens_output=400,
                    cost_usd=_C3,
                    duration_ms=1500,
                    status="succeeded",
                    started_at=now,
                ),
            ]
        )
        session.add_all(
            [
                SkillCall(agent_invocation_id="inv1", skill_name="edit_file"),
                SkillCall(agent_invocation_id="inv1", skill_name="bash"),
                SkillCall(agent_invocation_id="inv3", skill_name="grep"),
            ]
        )
        # Runs: 2 success (100ms, 300ms) + 1 failed (200ms) on sales/prod;
        # 1 crashed (no duration) on orders/dev.
        session.add_all(
            [
                Run(
                    id="r1",
                    kind="run",
                    target_id="b1",
                    pipeline_name="sales",
                    target="prod",
                    status="success",
                    duration_ms=100,
                    created_at=now,
                ),
                Run(
                    id="r2",
                    kind="run",
                    target_id="b1",
                    pipeline_name="sales",
                    target="prod",
                    status="success",
                    duration_ms=300,
                    created_at=now,
                ),
                Run(
                    id="r3",
                    kind="run",
                    target_id="b1",
                    pipeline_name="sales",
                    target="prod",
                    status="failed",
                    duration_ms=200,
                    created_at=now,
                ),
                Run(
                    id="r4",
                    kind="run",
                    target_id="b2",
                    pipeline_name="orders",
                    target="dev",
                    status="crashed",
                    duration_ms=None,
                    created_at=now,
                ),
            ]
        )
        session.commit()


def test_costs_sums_token_to_usd(postgres_state_store_url: str) -> None:
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        _seed(session_factory)
        rollup = MetricsRollups(session_factory).costs()
        assert rollup.invocations == 3
        assert rollup.tokens_input == 3500
        assert rollup.tokens_output == 700
        assert rollup.cost_usd == pytest.approx(_C1 + _C2 + _C3)
    finally:
        engine.dispose()


def test_runs_success_failure_and_durations(postgres_state_store_url: str) -> None:
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        _seed(session_factory)
        rollup = MetricsRollups(session_factory).runs()
        assert rollup.total == 4
        assert rollup.succeeded == 2
        assert rollup.failed == 2  # failed + crashed
        assert rollup.median_duration_ms == pytest.approx(200.0)
        # durations = [100, 200, 300] (crashed run has NULL duration, ignored).
        # Postgres ``percentile_cont(0.95)`` interpolates: 200 + 0.9*(300-200) =
        # 290.0 (the old nearest-rank Python helper returned 300.0 — the SQL
        # aggregation is authoritative now, per the F6 refactor).
        assert rollup.p95_duration_ms == pytest.approx(290.0)

        by = {(g.pipeline_name, g.target): g for g in rollup.by_target}
        assert by[("sales", "prod")].total == 3
        assert by[("sales", "prod")].succeeded == 2
        assert by[("sales", "prod")].failed == 1
        assert by[("orders", "dev")].total == 1
        assert by[("orders", "dev")].failed == 1
        # Ordered by (pipeline, target): orders before sales.
        assert [g.pipeline_name for g in rollup.by_target] == ["orders", "sales"]
    finally:
        engine.dispose()


def test_agents_aggregates_per_agent_usage(postgres_state_store_url: str) -> None:
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        _seed(session_factory)
        usages = {u.agent_name: u for u in MetricsRollups(session_factory).agents()}

        dlt = usages["dlt-engineer"]
        assert dlt.invocations == 2
        assert dlt.tokens_input == 1500
        assert dlt.tokens_output == 300
        assert dlt.cost_usd == pytest.approx(_C1 + _C2)
        assert dlt.succeeded == 1
        assert dlt.success_rate == pytest.approx(0.5)
        assert dlt.skill_calls == 2

        dbt = usages["dbt-engineer"]
        assert dbt.invocations == 1
        assert dbt.succeeded == 1
        assert dbt.success_rate == pytest.approx(1.0)
        assert dbt.skill_calls == 1

        # Busiest agent first.
        assert MetricsRollups(session_factory).agents()[0].agent_name == "dlt-engineer"
    finally:
        engine.dispose()


def test_since_window_filters_old_invocations(postgres_state_store_url: str) -> None:
    session_factory, engine = _prepare_db(postgres_state_store_url)
    try:
        _seed(session_factory)
        with session_factory() as session:
            session.add(
                AgentInvocation(
                    id="inv_old",
                    agent_name="dlt-engineer",
                    tokens_input=9999,
                    tokens_output=0,
                    cost_usd=1.23,
                    status="succeeded",
                    started_at=datetime.now(UTC) - timedelta(days=30),
                )
            )
            session.commit()

        service = MetricsRollups(session_factory)
        assert service.costs().invocations == 4  # includes the old row
        assert service.costs(parse_since("1h")).invocations == 3  # excludes it
    finally:
        engine.dispose()


def test_parse_since_variants() -> None:
    now = datetime.now(UTC)
    assert (now - parse_since("1h")) >= timedelta(minutes=59)
    assert (now - parse_since("7d")) >= timedelta(days=6, hours=23)
    assert (now - parse_since("30m")) >= timedelta(minutes=29)
    assert (now - parse_since("2w")) >= timedelta(days=13)
    with pytest.raises(ValueError, match="invalid --since"):
        parse_since("soon")
    with pytest.raises(ValueError):
        parse_since("10y")
