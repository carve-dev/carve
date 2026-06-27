"""The persisting StepSink writes + transitions step_runs rows.

Postgres-fixture-gated (the sink writes to the real ``step_runs`` table).
``step_started`` inserts a ``running`` row; ``step_finished`` transitions it to
the step's terminal status with the threaded outputs / error / timings. The
sink's async hooks bridge to the sync ``JobQueue`` via ``asyncio.to_thread``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from carve.core.config.pipeline_schema import DltStepConfig
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.runtime.persisting_step_sink import PersistingStepSink
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_executor import StepResult


@pytest.fixture
def queue(postgres_state_store_url: str) -> JobQueue:
    config = Config(
        project=ProjectConfig(name="sink-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return JobQueue(create_session_factory(engine))


def _run_id(queue: JobQueue) -> str:
    return Repository(queue._session_factory).create_run("pipeline", "job-x", target="dev")


async def test_step_started_then_finished_persists_a_succeeded_step_run(queue: JobQueue) -> None:
    run_id = _run_id(queue)
    sink = PersistingStepSink(run_id=run_id, job_queue=queue)
    step = DltStepConfig(id="ingest", component="stripe")
    run = PipelineRun(pipeline="p", target="dev", id=run_id)

    await sink.step_started(step=step, run=run, attempt=1)
    rows = queue.list_step_runs(run_id)
    assert len(rows) == 1
    assert rows[0].status == "running"
    assert rows[0].started_at is not None

    result = StepResult(
        status="succeeded",
        outputs={"tables": ["charges"]},
        duration_ms=42,
        finished_at=datetime.now(UTC),
    )
    await sink.step_finished(step=step, run=run, result=result, attempt=1)

    rows = queue.list_step_runs(run_id)
    assert len(rows) == 1  # the same row was transitioned, not a second insert
    row = rows[0]
    assert row.step_id == "ingest"
    assert row.step_type == "dlt"
    assert row.status == "succeeded"
    assert row.outputs == {"tables": ["charges"]}
    assert row.duration_ms == 42
    assert row.error_message is None
    assert row.finished_at is not None


async def test_step_finished_records_failure_with_error_message(queue: JobQueue) -> None:
    run_id = _run_id(queue)
    sink = PersistingStepSink(run_id=run_id, job_queue=queue)
    step = DltStepConfig(id="boom", component="stripe")
    run = PipelineRun(pipeline="p", target="dev", id=run_id)

    await sink.step_started(step=step, run=run, attempt=1)
    await sink.step_finished(
        step=step,
        run=run,
        result=StepResult(status="failed", error_message="kaboom"),
        attempt=1,
    )

    row = queue.list_step_runs(run_id)[0]
    assert row.status == "failed"
    assert row.error_message == "kaboom"


async def test_retries_record_one_step_run_per_attempt(queue: JobQueue) -> None:
    run_id = _run_id(queue)
    sink = PersistingStepSink(run_id=run_id, job_queue=queue)
    step = DltStepConfig(id="flaky", component="stripe")
    run = PipelineRun(pipeline="p", target="dev", id=run_id)

    # Attempt 1 fails, attempt 2 succeeds — each is its own step_runs row.
    await sink.step_started(step=step, run=run, attempt=1)
    await sink.step_finished(
        step=step, run=run, result=StepResult(status="failed", error_message="x"), attempt=1
    )
    await sink.step_started(step=step, run=run, attempt=2)
    await sink.step_finished(step=step, run=run, result=StepResult(status="succeeded"), attempt=2)

    rows = sorted(queue.list_step_runs(run_id), key=lambda r: r.attempt)
    assert [r.attempt for r in rows] == [1, 2]
    assert [r.status for r in rows] == ["failed", "succeeded"]
