"""The slice's headline: enqueue → worker claims → run + step_runs persisted.

Creds-free over DuckDB (the registry is wired with the same fake dlt/dbt/sql
seams as ``tests/runtime/step_types/test_registry_end_to_end.py``). Asserts the
end-to-end bar: the worker claims a manual job, creates a ``runs`` row, runs
``execute_pipeline`` with the **real persisting** ``StepSink``, writes a
``step_runs`` row per step (terminal status + outputs + timings), and marks the
``runs`` row + the ``jobs`` row terminal. Plus idempotency: a second ``run_once``
against an empty queue no-ops.

Postgres-fixture-gated (the queue + step_runs persistence are Postgres-only).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    DuckDBConnection,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.config.state_store import StateStoreConfig
from carve.core.dbt_execution.backend import DbtCommand
from carve.core.dbt_execution.result import STATUS_SUCCESS, DbtRunResult, PerModelResult
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.core.state.models import Event
from carve.runtime.events import EventEmitter
from carve.runtime.step_types.connections import ResolvedConnection
from carve.runtime.step_types.dlt import DltRunOutcome
from carve.runtime.step_types.registry import build_step_executor_registry
from carve.runtime.worker import WorkerContext, run_once

_PIPELINE_TOML = """
[pipeline]
description = "ingest + stage + refresh"

[[steps]]
id = "ingest_stripe"
type = "dlt"
component = "stripe_charges"
depends_on = []

[[steps]]
id = "stage_stripe"
type = "dbt"
command = "build"
select = "stg_stripe_charges+"
depends_on = ["ingest_stripe"]

[[steps]]
id = "refresh_search"
type = "sql"
file = "sql/refresh.sql"
connection = "local"
depends_on = ["stage_stripe"]
[steps.jinja_vars]
loaded_rows = "{{ steps.ingest_stripe.outputs.tables | length }}"
"""


def _project(tmp_path: Path) -> ProjectPaths:
    (tmp_path / "el" / "stripe_charges" / "scripts").mkdir(parents=True)
    (tmp_path / "el" / "stripe_charges" / "scripts" / "__init__.py").write_text(
        "def run():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "dbt_project.yml").write_text("name: analytics\n", encoding="utf-8")
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "stripe.toml").write_text(_PIPELINE_TOML, encoding="utf-8")
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "refresh.sql").write_text(
        "SELECT {{ vars.loaded_rows }} AS loaded_rows", encoding="utf-8"
    )
    return ProjectPaths.from_root(tmp_path)


def _dlt_run_fn() -> Any:
    import time as _time

    def _run(**kwargs: Any) -> DltRunOutcome:
        data_dir = Path(kwargs["env"]["DLT_DATA_DIR"])
        load_id = str(_time.time())
        pkg = data_dir / "pipelines" / "stripe_charges" / "load" / "loaded" / load_id
        (pkg / "completed_jobs").mkdir(parents=True)
        for t in ("charges", "_dlt_pipeline_state"):
            (pkg / "completed_jobs" / f"{t}.hash.0.insert_values.gz").write_text("")
        (pkg / "applied_schema_updates.json").write_text(json.dumps({"charges": {"columns": {}}}))
        (pkg / "load_package_state.json").write_text(
            json.dumps(
                {"load_metrics": {"charges.h.gz": {"table_name": "charges", "state": "completed"}}}
            )
        )
        return DltRunOutcome(returncode=0, output="loaded", duration_ms=3)

    return _run


def _dbt_backend_factory() -> Any:
    class _Backend:
        def run(self, command: DbtCommand) -> DbtRunResult:
            return DbtRunResult(
                status=STATUS_SUCCESS,
                per_model=[
                    PerModelResult(
                        unique_id="model.a.stg", name="stg_stripe_charges", status="success"
                    ),
                ],
                duration_ms=7,
            )

    def _factory(**_kwargs: Any) -> _Backend:
        return _Backend()

    return _factory


def _duckdb_factory() -> Any:
    from carve.core.connectors.duckdb import DIALECT
    from carve.core.connectors.duckdb import DuckDBConnection as DuckConn

    resolved = ResolvedConnection(DuckConn(database=":memory:"), DIALECT)

    def _factory(_name: str, _config: ConnectionsConfig) -> ResolvedConnection:
        return resolved

    return _factory


def _registry_factory(connections: ConnectionsConfig) -> Any:
    def _build() -> Any:
        return build_step_executor_registry(
            connections=connections,
            dbt_executable="dbt",
            dlt_run_fn=_dlt_run_fn(),
            dbt_backend_factory=_dbt_backend_factory(),
            connection_factory=_duckdb_factory(),
        )

    return _build


@pytest.fixture
def worker_context(tmp_path: Path, postgres_state_store_url: str) -> WorkerContext:
    config = Config(
        project=ProjectConfig(name="worker-e2e"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    factory = create_session_factory(engine)
    connections = ConnectionsConfig(duckdb={"local": DuckDBConnection()})
    return WorkerContext(
        repository=Repository(factory),
        job_queue=JobQueue(factory),
        paths=_project(tmp_path),
        connections=connections,
        dbt_executable="dbt",
        worker_id="test-worker",
        registry_factory=_registry_factory(connections),
    )


async def test_worker_runs_a_queued_job_end_to_end(worker_context: WorkerContext) -> None:
    queue = worker_context.job_queue
    repo = worker_context.repository

    job = queue.enqueue_manual("stripe", "dev", trigger="manual")

    ran = await run_once(worker_context)
    assert ran is True

    # The job is terminal `succeeded` and bound to a run.
    finished_job = queue.get_job(job.id)
    assert finished_job is not None
    assert finished_job.status == "succeeded"
    assert finished_job.run_id is not None

    # The run is terminal `success`.
    run = repo.get_run(finished_job.run_id)
    assert run is not None
    assert run.status == "success"

    # One step_runs row per step, all `succeeded`, with outputs + timings.
    step_runs = queue.list_step_runs(finished_job.run_id)
    by_step = {sr.step_id: sr for sr in step_runs}
    assert set(by_step) == {"ingest_stripe", "stage_stripe", "refresh_search"}
    for sr in step_runs:
        assert sr.status == "succeeded"
        assert sr.started_at is not None
        assert sr.finished_at is not None
    # The dlt step's outputs threaded into step_runs.
    assert by_step["ingest_stripe"].outputs["tables"] == ["charges"]


async def test_run_once_on_empty_queue_is_a_noop(worker_context: WorkerContext) -> None:
    assert await run_once(worker_context) is False


async def test_second_run_once_after_completion_claims_nothing(
    worker_context: WorkerContext,
) -> None:
    worker_context.job_queue.enqueue_manual("stripe", "dev", trigger="manual")
    assert await run_once(worker_context) is True
    # The queue is now empty (the job is terminal); a re-claim is idempotent.
    assert await run_once(worker_context) is False


async def test_heartbeat_advances_during_execute_then_stops_at_completion(
    worker_context: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker beats while a job executes, and the heartbeat stops at the end.

    Replaces ``_execute_job`` with a stub that awaits until it observes the DB
    ``heartbeat_at`` advance past the claim's stamp (proving the heartbeat loop is
    running concurrently with execution), then returns success. A ``FakeClock``
    drives the heartbeat sleep-free. After completion, ``heartbeat_at`` no longer
    advances — the handle was stopped in the worker's ``finally``.
    """
    import carve.runtime.worker as worker_mod
    from carve.runtime.clock import FakeClock
    from carve.runtime.execute_pipeline import RunResult

    queue = worker_context.job_queue
    job = queue.enqueue_manual("stripe", "dev", trigger="manual")

    clock = FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    ctx = WorkerContext(
        repository=worker_context.repository,
        job_queue=queue,
        paths=worker_context.paths,
        connections=worker_context.connections,
        dbt_executable=worker_context.dbt_executable,
        worker_id="test-worker",
        registry_factory=worker_context.registry_factory,
        clock=clock,
    )

    async def _slow_execute(
        c: WorkerContext, *, job_pipeline: str, run_id: str, target: str
    ) -> Any:
        # Wait until at least 2 heartbeats land past the claim stamp (the loop is
        # running concurrently). The FakeClock advances each beat, so heartbeat_at
        # moves forward; bound the wait so a broken heartbeat fails the test.
        seen: set[Any] = set()
        for _ in range(2000):
            hb = queue.get_job(job.id)
            if hb is not None and hb.heartbeat_at is not None:
                seen.add(hb.heartbeat_at)
            if len(seen) >= 2:
                break
            await asyncio.sleep(0)
        assert len(seen) >= 2, "heartbeat_at did not advance during execute"
        return RunResult(
            status="succeeded",
            completed=frozenset({"only"}),
            failed=frozenset(),
            skipped=frozenset(),
        )

    monkeypatch.setattr(worker_mod, "_execute_job", _slow_execute)

    ran = await run_once(ctx)
    assert ran is True

    # The job is terminal succeeded — the guarded mark_finished (with the worker's
    # id) landed, and the heartbeat handle was stopped at completion.
    finished = queue.get_job(job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    hb_at_completion = finished.heartbeat_at

    # No further beats after completion: heartbeat_at is stable across many loop
    # turns (the handle was cancelled in the worker's finally).
    for _ in range(50):
        await asyncio.sleep(0)
    after = queue.get_job(job.id)
    assert after is not None
    assert after.heartbeat_at == hb_at_completion


async def test_worker_guarded_writes_noop_when_reclaimed_mid_run(
    worker_context: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker reclaimed mid-execute can't stomp the job — its finalize no-ops.

    Simulates the zombie race end-to-end: while worker A executes, the job is
    reclaimed (back to queued) and re-claimed + transitioned by worker B. Worker
    A's guarded ``mark_finished(expected_worker_id=A)`` then matches 0 rows, so
    B's state survives — no double-finalize.
    """
    import carve.runtime.worker as worker_mod
    from carve.runtime.execute_pipeline import RunResult

    queue = worker_context.job_queue
    repo = worker_context.repository
    job = queue.enqueue_manual("stripe", "dev", trigger="manual")

    async def _execute_then_get_reclaimed(
        c: WorkerContext, *, job_pipeline: str, run_id: str, target: str
    ) -> Any:
        # Mid-run, the reaper reclaims A's job (→ queued) and worker B re-claims +
        # transitions it. A is now a zombie.
        await asyncio.to_thread(queue.reclaim_stale, datetime.now(UTC), stale_threshold_s=-1.0)
        await asyncio.to_thread(queue.claim_next, "worker-B")
        run_b = await asyncio.to_thread(repo.create_run, "pipeline", job.id, target="dev")
        await asyncio.to_thread(
            queue.transition_to_running, job.id, run_b, expected_worker_id="worker-B"
        )
        return RunResult(
            status="succeeded",
            completed=frozenset({"only"}),
            failed=frozenset(),
            skipped=frozenset(),
        )

    monkeypatch.setattr(worker_mod, "_execute_job", _execute_then_get_reclaimed)

    ran = await run_once(worker_context)  # worker_id="test-worker" (worker A)
    assert ran is True

    # Worker A's finalize no-opped: the job is still B's running claim, not A's
    # terminal succeeded.
    back = queue.get_job(job.id)
    assert back is not None
    assert back.status == "running"
    assert back.claimed_by == "worker-B"


def _events(ctx: WorkerContext, kind: str) -> list[Event]:
    stmt = sa.select(Event).where(Event.kind == kind).order_by(Event.id.asc())
    with ctx.job_queue._session_factory() as session:
        return list(session.scalars(stmt).all())


def _emitting_ctx(base: WorkerContext, **overrides: Any) -> WorkerContext:
    """Clone ``base`` with a live :class:`EventEmitter` injected (+ overrides)."""
    factory = base.job_queue._session_factory
    fields: dict[str, Any] = {
        "repository": base.repository,
        "job_queue": base.job_queue,
        "paths": base.paths,
        "connections": base.connections,
        "dbt_executable": base.dbt_executable,
        "worker_id": "test-worker",
        "registry_factory": base.registry_factory,
        "emitter": EventEmitter(factory),
    }
    fields.update(overrides)
    return WorkerContext(**fields)


async def test_successful_run_persists_run_started_and_succeeded(
    worker_context: WorkerContext,
) -> None:
    """A full worker run persists ``run.started`` then ``run.succeeded``."""
    ctx = _emitting_ctx(worker_context)
    job = ctx.job_queue.enqueue_manual("stripe", "dev", trigger="manual")

    assert await run_once(ctx) is True

    started = _events(ctx, "run.started")
    assert len(started) == 1
    assert started[0].payload == {
        "run_id": ctx.job_queue.get_job(job.id).run_id,  # type: ignore[union-attr]
        "job_id": job.id,
        "pipeline": "stripe",
    }
    succeeded = _events(ctx, "run.succeeded")
    assert len(succeeded) == 1
    assert succeeded[0].payload["job_id"] == job.id
    assert succeeded[0].payload["error_message"] is None
    assert succeeded[0].payload["duration_ms"] is not None
    # A success never emits run.failed.
    assert _events(ctx, "run.failed") == []


async def test_failed_run_persists_run_failed_and_fires_hook(
    worker_context: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing run persists ``run.failed`` AND fires the ``on_run_failed`` hook.

    The two mechanisms are distinct and both fire at the same transition: the
    durable event (observability) and the user hook (a notify). Execution is
    stubbed to return a failed :class:`RunResult` so the failure is deterministic.
    """
    import carve.runtime.worker as worker_mod
    from carve.runtime.execute_pipeline import RunResult

    fired: list[dict[str, Any]] = []

    def _hook(payload: dict[str, Any]) -> None:
        fired.append(payload)

    ctx = _emitting_ctx(worker_context, on_run_failed=_hook)
    job = ctx.job_queue.enqueue_manual("stripe", "dev", trigger="manual")

    async def _fail_execute(
        c: WorkerContext, *, job_pipeline: str, run_id: str, target: str
    ) -> Any:
        return RunResult(
            status="failed",
            completed=frozenset(),
            failed=frozenset({"s"}),
            skipped=frozenset(),
        )

    monkeypatch.setattr(worker_mod, "_execute_job", _fail_execute)

    assert await run_once(ctx) is True

    # The durable run.failed event landed.
    failed = _events(ctx, "run.failed")
    assert len(failed) == 1
    assert failed[0].payload["job_id"] == job.id
    assert failed[0].payload["pipeline"] == "stripe"

    # The user on_run_failed hook fired, with the {pipeline}/{run_id}/{target}/{error} keys.
    assert len(fired) == 1
    assert fired[0]["pipeline"] == "stripe"
    assert fired[0]["target"] == "dev"
    assert "run_id" in fired[0]
    assert "error" in fired[0]

    # The job ends terminal failed.
    finished = ctx.job_queue.get_job(job.id)
    assert finished is not None
    assert finished.status == "failed"


async def test_raising_on_run_failed_hook_is_surfaced_not_fatal(
    worker_context: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising ``on_run_failed`` hook is logged, not fatal — the run stays failed.

    Post-event semantics (like ``post_build``): the run already failed, so a hook
    that raises does NOT propagate out of ``run_once`` and does NOT change the
    terminal state.
    """
    import carve.runtime.worker as worker_mod
    from carve.runtime.execute_pipeline import RunResult

    def _boom(payload: dict[str, Any]) -> None:
        raise RuntimeError("notify-slack exploded")

    ctx = _emitting_ctx(worker_context, on_run_failed=_boom)
    job = ctx.job_queue.enqueue_manual("stripe", "dev", trigger="manual")

    async def _fail_execute(
        c: WorkerContext, *, job_pipeline: str, run_id: str, target: str
    ) -> Any:
        return RunResult(
            status="failed",
            completed=frozenset(),
            failed=frozenset({"s"}),
            skipped=frozenset(),
        )

    monkeypatch.setattr(worker_mod, "_execute_job", _fail_execute)

    # run_once must NOT raise despite the raising hook.
    assert await run_once(ctx) is True
    finished = ctx.job_queue.get_job(job.id)
    assert finished is not None
    assert finished.status == "failed"  # the run stays terminal-failed


async def test_setup_failure_on_claimed_job_marks_failed_not_orphaned(
    worker_context: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB error during setup (create_run) resolves the claim → job `failed`.

    Once claimed, a job is ours; a failure anywhere after the claim — even in the
    setup writes before execute — must mark it terminal, never leave it orphaned
    `claimed`. The reaper that would otherwise reclaim a stuck job is deferred
    this slice, so an orphan would block the pipeline forever.
    """
    queue = worker_context.job_queue
    job = queue.enqueue_manual("stripe", "dev", trigger="manual")

    def _boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("transient db error during create_run")

    monkeypatch.setattr(worker_context.repository, "create_run", _boom)

    # The claimed job is handled (not skipped) and ends terminal `failed`.
    assert await run_once(worker_context) is True
    finished = queue.get_job(job.id)
    assert finished is not None
    assert finished.status == "failed"
