"""The reaper's reclaim — stale jobs reclaimed, fresh ones left, no double-reclaim.

Postgres-fixture-gated: ``reclaim_stale`` is a raw ``UPDATE ... RETURNING`` over
the real ``jobs`` table + its partial indexes, which SQLite can't express. The
stale state is *planted* (a ``heartbeat_at`` 70s in the past) rather than waited
for — the deterministic, sleep-free equivalent of a crashed worker, per the
Acceptance "no timing flakes" bar. Covers:

* a ``claimed`` / ``running`` job with a STALE heartbeat is reclaimed → ``queued``,
  claim + heartbeat cleared, and its in-flight Run failed via the reaper pass;
* a job with a FRESH heartbeat is NOT reclaimed;
* ``run_id`` stays on the reclaimed job (audit), is RETURNED to the caller;
* N concurrent ``reclaim_stale`` calls reclaim each stale job EXACTLY once (the
  atomic single-statement reclaim — no double-reclaim).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.core.state.models import Job
from carve.runtime.reaper import reap_stale_once


@pytest.fixture
def queue(postgres_state_store_url: str) -> JobQueue:
    config = Config(
        project=ProjectConfig(name="reaper-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    engine.dispose()
    pooled = create_engine_from_config(config)
    initialize_database(pooled)
    return JobQueue(create_session_factory(pooled))


def _repo_for(queue: JobQueue) -> Repository:
    return Repository(queue._session_factory)


def _plant_claimed(
    queue: JobQueue,
    *,
    pipeline: str,
    status: str,
    heartbeat_age_s: float,
    worker_id: str = "worker-A",
    run_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Force a job into ``claimed``/``running`` with a planted ``heartbeat_at``.

    Enqueues + claims so the partial indexes are satisfied, then directly stamps
    ``status``/``heartbeat_at``/``run_id`` to simulate a worker that beat
    ``heartbeat_age_s`` seconds ago (stale if > threshold). Returns the job id.
    """
    now = now if now is not None else datetime.now(UTC)
    job = queue.enqueue_scheduled(pipeline, "dev")
    queue.claim_next(worker_id)
    with queue._session_factory() as session:
        row = session.get(Job, job.id)
        assert row is not None
        row.status = status
        row.claimed_by = worker_id
        row.heartbeat_at = now - timedelta(seconds=heartbeat_age_s)
        row.run_id = run_id
        session.commit()
    return job.id


def test_stale_claimed_job_is_reclaimed_and_its_run_failed(queue: JobQueue) -> None:
    repo = _repo_for(queue)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    # An in-flight run for the stale job (so the reaper has a run to fail).
    job = queue.enqueue_scheduled("stale", "dev")
    queue.claim_next("worker-A")
    run_id = repo.create_run("pipeline", job.id, target="dev")
    repo.update_run_status(run_id, "running")
    with queue._session_factory() as session:
        row = session.get(Job, job.id)
        assert row is not None
        row.status = "running"
        row.claimed_by = "worker-A"
        row.run_id = run_id
        row.heartbeat_at = now - timedelta(seconds=70)  # stale (> 60s)
        session.commit()

    reclaimed = reap_stale_once(queue, repo, now, stale_threshold_s=60.0)
    assert reclaimed == [job.id]

    back = queue.get_job(job.id)
    assert back is not None
    assert back.status == "queued"
    assert back.claimed_by is None
    assert back.claimed_at is None
    assert back.heartbeat_at is None
    # run_id stays on the job for audit (NOT nulled).
    assert back.run_id == run_id

    # The orphaned in-flight run is terminal failed with the reclaim reason.
    failed_run = repo.get_run(run_id)
    assert failed_run is not None
    assert failed_run.status == "failed"
    assert failed_run.error_message == "worker_crashed_or_unreachable"


def test_fresh_heartbeat_job_is_not_reclaimed(queue: JobQueue) -> None:
    repo = _repo_for(queue)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    job_id = _plant_claimed(
        queue, pipeline="fresh", status="running", heartbeat_age_s=30.0, now=now
    )

    reclaimed = reap_stale_once(queue, repo, now, stale_threshold_s=60.0)
    assert reclaimed == []

    back = queue.get_job(job_id)
    assert back is not None
    assert back.status == "running"
    assert back.claimed_by == "worker-A"


def test_reclaimed_job_with_null_run_id_skips_run_fail(queue: JobQueue) -> None:
    # A claimed-but-never-transitioned job has run_id NULL — the reaper reclaims
    # it but has no run to fail, and must not error.
    repo = _repo_for(queue)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    job_id = _plant_claimed(
        queue, pipeline="norun", status="claimed", heartbeat_age_s=90.0, run_id=None, now=now
    )

    reclaimed = reap_stale_once(queue, repo, now, stale_threshold_s=60.0)
    assert reclaimed == [job_id]
    back = queue.get_job(job_id)
    assert back is not None
    assert back.status == "queued"


def test_job_reclaimed_event_emitted_via_seam(queue: JobQueue) -> None:
    repo = _repo_for(queue)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    emitted: list[tuple[str, dict[str, object]]] = []
    original = queue._emit

    def spy(kind: str, payload: dict[str, object]) -> None:
        emitted.append((kind, payload))
        original(kind, payload)

    queue._emit = spy  # type: ignore[method-assign]

    job_id = _plant_claimed(queue, pipeline="evt", status="claimed", heartbeat_age_s=70.0, now=now)
    reap_stale_once(queue, repo, now, stale_threshold_s=60.0)

    reclaimed_events = [e for e in emitted if e[0] == "job.reclaimed"]
    assert len(reclaimed_events) == 1
    payload = reclaimed_events[0][1]
    assert payload["job_id"] == job_id
    assert payload["prior_claimed_by"] == "worker-A"
    assert payload["reason"] == "stale_heartbeat"


def test_concurrent_reclaim_stale_reclaims_each_job_exactly_once(queue: JobQueue) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    # Plant several distinct stale jobs (distinct pipelines so the partial unique
    # indexes don't collide).
    job_ids = {
        _plant_claimed(
            queue,
            pipeline=f"race-{i}",
            status="running",
            heartbeat_age_s=70.0,
            worker_id=f"worker-{i}",
            now=now,
        )
        for i in range(6)
    }

    n = 6
    barrier = threading.Barrier(n)
    reclaimed_all: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()  # all reapers race reclaim_stale on the same stale set
        rows = queue.reclaim_stale(now, stale_threshold_s=60.0)
        with lock:
            reclaimed_all.extend(job_id for job_id, _run, _by in rows)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda _: attempt(), range(n)))

    # Every stale job was reclaimed by EXACTLY one reaper — no duplicates.
    assert sorted(reclaimed_all) == sorted(job_ids)
    assert len(reclaimed_all) == len(set(reclaimed_all))
