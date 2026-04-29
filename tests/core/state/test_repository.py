"""Repository tests: create, list, update runs; append/read logs; plan round-trip."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
from carve.core.state.models import Plan


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    config = Config(
        project=ProjectConfig(name="repo-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(state_store="sqlite:///.carve/state.db"),
    )
    engine = create_engine_from_config(config, project_dir=tmp_path)
    initialize_database(engine)
    factory = create_session_factory(engine)
    return Repository(factory)


# ----------------------------------------------------------------------- Runs


def test_create_run_returns_id_and_persists(repo: Repository) -> None:
    run_id = repo.create_run(kind="apply", target_id="plan-abc")
    assert isinstance(run_id, str)
    assert len(run_id) == 32  # uuid4().hex

    run = repo.get_run(run_id)
    assert run is not None
    assert run.kind == "apply"
    assert run.target_id == "plan-abc"
    assert run.status == "queued"
    assert run.tokens_input == 0
    assert run.tokens_output == 0
    assert run.cost_usd == 0.0


def test_get_run_returns_none_for_unknown_id(repo: Repository) -> None:
    assert repo.get_run("does-not-exist") is None


def test_update_run_status_to_running_sets_started_at(repo: Repository) -> None:
    run_id = repo.create_run(kind="pipeline", target_id="my_pipeline")
    repo.update_run_status(run_id, "running")

    run = repo.get_run(run_id)
    assert run is not None
    assert run.status == "running"
    assert run.started_at is not None
    assert run.completed_at is None


def test_update_run_status_terminal_sets_completed_and_duration(repo: Repository) -> None:
    run_id = repo.create_run(kind="apply", target_id="plan-1")
    repo.update_run_status(run_id, "running")
    time.sleep(0.01)
    repo.update_run_status(run_id, "success")

    run = repo.get_run(run_id)
    assert run is not None
    assert run.status == "success"
    assert run.completed_at is not None
    assert run.duration_ms is not None
    assert run.duration_ms >= 0


def test_update_run_status_failed_records_error(repo: Repository) -> None:
    run_id = repo.create_run(kind="apply", target_id="plan-1")
    repo.update_run_status(run_id, "running")
    repo.update_run_status(run_id, "failed", error="snowflake exploded")

    run = repo.get_run(run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.error_message == "snowflake exploded"


def test_update_run_status_unknown_id_raises(repo: Repository) -> None:
    with pytest.raises(KeyError):
        repo.update_run_status("nope", "running")


def test_list_runs_respects_status_filter(repo: Repository) -> None:
    a = repo.create_run("apply", "p1")
    b = repo.create_run("apply", "p2")
    c = repo.create_run("apply", "p3")
    repo.update_run_status(a, "running")
    repo.update_run_status(b, "running")
    repo.update_run_status(b, "success")

    queued = repo.list_runs(status="queued")
    assert {r.id for r in queued} == {c}

    successful = repo.list_runs(status="success")
    assert {r.id for r in successful} == {b}


def test_list_runs_respects_limit(repo: Repository) -> None:
    ids = [repo.create_run("apply", f"p{i}") for i in range(5)]
    assert len(ids) == 5

    runs = repo.list_runs(limit=3)
    assert len(runs) == 3


def test_list_runs_orders_newest_first(repo: Repository) -> None:
    a = repo.create_run("apply", "p1")
    time.sleep(0.005)
    b = repo.create_run("apply", "p2")
    time.sleep(0.005)
    c = repo.create_run("apply", "p3")

    runs = repo.list_runs()
    assert [r.id for r in runs] == [c, b, a]


# ----------------------------------------------------------------------- Logs


def test_append_log_preserves_order(repo: Repository) -> None:
    run_id = repo.create_run("apply", "plan-1")
    for i in range(10):
        repo.append_log(run_id, "info", "agent", f"line-{i}")

    logs = repo.get_logs(run_id)
    assert [log.message for log in logs] == [f"line-{i}" for i in range(10)]


def test_get_logs_filters_by_since_exclusive(repo: Repository) -> None:
    run_id = repo.create_run("apply", "plan-1")
    repo.append_log(run_id, "info", "agent", "first")
    time.sleep(0.01)
    repo.append_log(run_id, "info", "agent", "second")

    all_logs = repo.get_logs(run_id)
    assert len(all_logs) == 2

    cutoff = all_logs[0].timestamp
    later = repo.get_logs(run_id, since=cutoff)
    assert [log.message for log in later] == ["second"]


def test_get_logs_returns_empty_for_unknown_run(repo: Repository) -> None:
    assert repo.get_logs("missing") == []


# ---------------------------------------------------------------------- Plans


def _make_plan(plan_id: str = "plan-001", **overrides: object) -> Plan:
    defaults: dict[str, object] = {
        "id": plan_id,
        "goal": "build the warehouse",
        "config_hash": "abc123",
        "carve_version": "0.0.1",
        "estimates_json": '{"cost_usd": 1.23}',
        "task_graph_json": '{"nodes": []}',
        "file_path": f".carve/plans/{plan_id}.json",
    }
    defaults.update(overrides)
    return Plan(**defaults)


def test_save_and_get_plan_round_trip(repo: Repository) -> None:
    repo.save_plan(_make_plan("plan-001"))

    fetched = repo.get_plan("plan-001")
    assert fetched is not None
    assert fetched.id == "plan-001"
    assert fetched.goal == "build the warehouse"
    assert fetched.task_graph_json == '{"nodes": []}'


def test_get_plan_returns_none_for_unknown(repo: Repository) -> None:
    assert repo.get_plan("nope") is None


def test_list_plans_orders_newest_first(repo: Repository) -> None:
    repo.save_plan(_make_plan("plan-1"))
    time.sleep(0.005)
    repo.save_plan(_make_plan("plan-2"))
    time.sleep(0.005)
    repo.save_plan(_make_plan("plan-3"))

    listed = repo.list_plans()
    assert [p.id for p in listed] == ["plan-3", "plan-2", "plan-1"]


def test_list_expired_plans_returns_only_old_unapplied(repo: Repository) -> None:
    now = datetime.now(UTC)
    fresh = _make_plan("fresh", expires_at=now + timedelta(hours=1))
    expired = _make_plan("expired", expires_at=now - timedelta(hours=1))
    applied = _make_plan(
        "applied",
        expires_at=now - timedelta(hours=1),
        applied_at=now - timedelta(minutes=30),
        apply_run_id=None,
    )
    repo.save_plan(fresh)
    repo.save_plan(expired)
    repo.save_plan(applied)

    expired_list = repo.list_expired_plans(now=now)
    assert [p.id for p in expired_list] == ["expired"]


def test_expire_old_plans_returns_count(repo: Repository) -> None:
    now = datetime.now(UTC)
    repo.save_plan(_make_plan("a", expires_at=now - timedelta(hours=2)))
    repo.save_plan(_make_plan("b", expires_at=now - timedelta(hours=1)))
    repo.save_plan(_make_plan("c", expires_at=now + timedelta(hours=1)))

    assert repo.expire_old_plans(now=now) == 2
