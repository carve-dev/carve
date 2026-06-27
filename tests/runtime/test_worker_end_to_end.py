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

import json
from pathlib import Path
from typing import Any

import pytest

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
