"""The load-bearing concurrency invariants — driven deterministically, no sleeps.

Postgres-fixture-gated: the partial unique index and ``FOR UPDATE SKIP LOCKED``
semantics do not exist in SQLite. Concurrency is driven with real threads (each
its own session against one shared database) synchronized by a ``Barrier`` so
the claims/enqueues genuinely race, and the assertion is the invariant
(exactly-one), not a timing window. Per the capability Acceptance bar, these
pass deterministically with no flaky sleeps.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import (
    JobQueue,
    PipelineAlreadyRunning,
    QueuedJobAlreadyExists,
)


@pytest.fixture
def queue(postgres_state_store_url: str) -> JobQueue:
    config = Config(
        project=ProjectConfig(name="claim-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    # A pool big enough that every concurrent worker gets a real connection
    # (otherwise threads serialize on the pool, defeating the race).
    engine = create_engine_from_config(config)
    engine.dispose()
    pooled = create_engine_from_config(config)
    initialize_database(pooled)
    return JobQueue(create_session_factory(pooled))


def _repo_for(queue: JobQueue) -> Repository:
    # The JobQueue and Repository share the same session factory.
    return Repository(queue._session_factory)


# --- (a) the partial-unique enqueue race --------------------------------------


def test_concurrent_enqueue_scheduled_yields_exactly_one_queued_job(queue: JobQueue) -> None:
    n = 8
    barrier = threading.Barrier(n)
    successes: list[str] = []
    conflicts = 0
    lock = threading.Lock()

    def attempt() -> None:
        nonlocal conflicts
        barrier.wait()  # release all threads into enqueue() simultaneously
        try:
            job = queue.enqueue_scheduled("races", "dev")
        except QueuedJobAlreadyExists:
            with lock:
                conflicts += 1
        else:
            with lock:
                successes.append(job.id)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda _: attempt(), range(n)))

    # Exactly one enqueue won; the other n-1 failed safe. No double-queue.
    assert len(successes) == 1
    assert conflicts == n - 1


# --- (b) FOR UPDATE SKIP LOCKED claim -----------------------------------------


def test_concurrent_claim_next_claims_a_job_exactly_once(queue: JobQueue) -> None:
    queue.enqueue_scheduled("only", "dev")

    n = 8
    barrier = threading.Barrier(n)
    claimed: list[str] = []
    nones = 0
    lock = threading.Lock()

    def attempt(worker_idx: int) -> None:
        nonlocal nones
        barrier.wait()  # all workers race claim_next on the single queued job
        job = queue.claim_next(f"worker-{worker_idx}")
        with lock:
            if job is None:
                nones += 1
            else:
                claimed.append(job.id)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(attempt, range(n)))

    # Exactly one worker claimed the job; the rest skipped the locked row and
    # got None (never blocked, never double-claimed).
    assert len(claimed) == 1
    assert nones == n - 1


def test_claim_next_on_empty_queue_returns_none(queue: JobQueue) -> None:
    assert queue.claim_next("solo") is None


def test_claim_next_skips_a_future_scheduled_job(queue: JobQueue) -> None:
    from datetime import UTC, datetime, timedelta

    future = datetime.now(UTC) + timedelta(hours=1)
    queue.enqueue_scheduled("later", "dev", scheduled_for=future)
    # Not yet due -> nothing claimable.
    assert queue.claim_next("w") is None


# --- (c) transition_to_running serialization ----------------------------------


def test_transition_to_running_raises_when_pipeline_already_running(queue: JobQueue) -> None:
    repo = _repo_for(queue)

    first = queue.enqueue_scheduled("serial", "dev")
    queue.claim_next("w1")
    run1 = repo.create_run("pipeline", first.id, target="dev")
    queue.transition_to_running(first.id, run1)

    # A second job for the same pipeline can queue (the first is now running,
    # not queued) and be claimed, but cannot transition to running.
    second = queue.enqueue_scheduled("serial", "dev")
    queue.claim_next("w2")
    run2 = repo.create_run("pipeline", second.id, target="dev")
    with pytest.raises(PipelineAlreadyRunning):
        queue.transition_to_running(second.id, run2)


def test_release_claim_returns_a_claimed_job_to_queued(queue: JobQueue) -> None:
    job = queue.enqueue_scheduled("releasable", "dev")
    claimed = queue.claim_next("w1")
    assert claimed is not None
    assert claimed.status == "claimed"

    queue.release_claim(job.id)
    back = queue.get_job(job.id)
    assert back is not None
    assert back.status == "queued"
    assert back.claimed_by is None

    # It is claimable again.
    reclaimed = queue.claim_next("w2")
    assert reclaimed is not None
    assert reclaimed.id == job.id
