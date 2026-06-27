"""Job-queue enqueue dedup: the partial unique index enforces at-most-one-queued.

Postgres-fixture-gated (the partial unique index doesn't exist in SQLite). A
second ``enqueue_scheduled`` for a pipeline that already has a queued job must
raise ``QueuedJobAlreadyExists`` (the index is the enforcer, never
check-then-insert); ``enqueue_manual`` instead upserts and returns the existing
queued job's id.
"""

from __future__ import annotations

import pytest

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue, QueuedJobAlreadyExists


@pytest.fixture
def queue(postgres_state_store_url: str) -> JobQueue:
    config = Config(
        project=ProjectConfig(name="queue-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return JobQueue(create_session_factory(engine))


def test_enqueue_scheduled_creates_a_queued_job(queue: JobQueue) -> None:
    job = queue.enqueue_scheduled("sales", "dev")
    assert job.pipeline == "sales"
    assert job.target == "dev"
    assert job.status == "queued"
    assert job.trigger == "scheduled"
    assert job.id.startswith("job_")


def test_enqueue_scheduled_twice_raises_queued_job_already_exists(queue: JobQueue) -> None:
    first = queue.enqueue_scheduled("sales", "dev")
    with pytest.raises(QueuedJobAlreadyExists):
        queue.enqueue_scheduled("sales", "dev")
    # The first job is untouched and still the only queued one.
    assert queue.get_job(first.id) is not None
    assert queue.get_job(first.id).status == "queued"


def test_enqueue_scheduled_different_pipelines_both_queue(queue: JobQueue) -> None:
    a = queue.enqueue_scheduled("sales", "dev")
    b = queue.enqueue_scheduled("marketing", "dev")
    assert a.id != b.id
    assert a.status == b.status == "queued"


def test_enqueue_manual_upsert_returns_existing_queued_id(queue: JobQueue) -> None:
    scheduled = queue.enqueue_scheduled("sales", "dev")
    # A manual trigger for the same pipeline coalesces onto the queued row.
    manual = queue.enqueue_manual("sales", "prod", trigger="manual")
    assert manual.id == scheduled.id
    # The upsert refreshed the trigger + target on the existing row.
    assert manual.trigger == "manual"
    assert manual.target == "prod"
    assert manual.scheduled_for is None


def test_enqueue_manual_on_empty_pipeline_inserts(queue: JobQueue) -> None:
    job = queue.enqueue_manual("fresh", "dev", trigger="api")
    assert job.status == "queued"
    assert job.trigger == "api"
