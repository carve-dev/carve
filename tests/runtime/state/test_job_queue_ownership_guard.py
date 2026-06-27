"""The worker-zombie ownership guard — a reclaimed worker can't stomp the new owner.

Postgres-fixture-gated: the guard is a raw conditional ``UPDATE ... WHERE
claimed_by=:worker AND status IN (...)`` over the real ``jobs`` table. The race
this defends against: worker A claims a job, stalls past the reaper threshold,
the reaper reclaims it (→ ``queued``), worker B re-claims + transitions it, and
THEN worker A returns and tries to finalize. A's guarded writes must no-op (0
rows matched) so B's state is intact — no double-finalize, no status stomp.

Covers:
* ``mark_finished(expected_worker_id=A)`` after reclaim+re-claim by B → no-op
  (returns ``False``, B's ``running`` state intact);
* ``transition_to_running(expected_worker_id=A)`` likewise → no-op;
* the OWNING worker's guarded writes DO land (returns ``True``);
* back-compat: ``expected_worker_id=None`` behaves exactly as the unguarded
  shipped path (writes unconditionally, ``KeyError`` on a missing job).
"""

from __future__ import annotations

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


@pytest.fixture
def queue(postgres_state_store_url: str) -> JobQueue:
    config = Config(
        project=ProjectConfig(name="guard-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return JobQueue(create_session_factory(engine))


def _repo_for(queue: JobQueue) -> Repository:
    return Repository(queue._session_factory)


def test_reclaimed_worker_mark_finished_is_a_noop(queue: JobQueue) -> None:
    repo = _repo_for(queue)

    # Worker A claims a job.
    job = queue.enqueue_scheduled("zombie", "dev")
    claimed = queue.claim_next("worker-A")
    assert claimed is not None and claimed.claimed_by == "worker-A"

    # The reaper reclaims it (stale) — back to queued, claim cleared.
    queue.release_claim(job.id)  # release_claim mirrors reclaim's queued+clear

    # Worker B re-claims and transitions it to running.
    reclaimed = queue.claim_next("worker-B")
    assert reclaimed is not None and reclaimed.claimed_by == "worker-B"
    run_b = repo.create_run("pipeline", job.id, target="dev")
    assert queue.transition_to_running(job.id, run_b, expected_worker_id="worker-B") is True

    # Worker A returns and tries to finalize — its guard matches 0 rows.
    landed = queue.mark_finished(job.id, "failed", expected_worker_id="worker-A")
    assert landed is False

    # B's running state is intact — A did not stomp it.
    back = queue.get_job(job.id)
    assert back is not None
    assert back.status == "running"
    assert back.claimed_by == "worker-B"
    assert back.run_id == run_b
    assert back.finished_at is None


def test_reclaimed_worker_heartbeat_is_a_noop(queue: JobQueue) -> None:
    # The heartbeat is the worker's last ownership-aware write: a returning
    # zombie's beat must not refresh a job another worker now owns.
    job = queue.enqueue_scheduled("zombie_hb", "dev")
    queue.claim_next("worker-A")
    queue.release_claim(job.id)  # reaper reclaims
    reclaimed = queue.claim_next("worker-B")
    assert reclaimed is not None and reclaimed.claimed_by == "worker-B"
    b_beat = queue.get_job(job.id)
    assert b_beat is not None
    b_heartbeat = b_beat.heartbeat_at

    # Worker A's heartbeat for the reclaimed job is a silent no-op (not its job).
    queue.update_heartbeat(job.id, expected_worker_id="worker-A")
    after = queue.get_job(job.id)
    assert after is not None
    assert after.heartbeat_at == b_heartbeat  # unchanged — A did not refresh B's job
    assert after.claimed_by == "worker-B"


def test_reclaimed_worker_transition_to_running_is_a_noop(queue: JobQueue) -> None:
    repo = _repo_for(queue)

    job = queue.enqueue_scheduled("zombie2", "dev")
    queue.claim_next("worker-A")
    # Reclaimed (stale) back to queued.
    queue.release_claim(job.id)
    # Worker B re-claims.
    reclaimed = queue.claim_next("worker-B")
    assert reclaimed is not None and reclaimed.claimed_by == "worker-B"

    # Worker A returns and tries to transition — its guard requires claimed_by=A
    # AND status='claimed'; the job is now claimed by B → 0 rows → no-op.
    run_a = repo.create_run("pipeline", job.id, target="dev")
    landed = queue.transition_to_running(job.id, run_a, expected_worker_id="worker-A")
    assert landed is False

    # The job is still B's claim, unflipped (A did not bind it to A's run).
    back = queue.get_job(job.id)
    assert back is not None
    assert back.status == "claimed"
    assert back.claimed_by == "worker-B"
    assert back.run_id != run_a


def test_owning_worker_guarded_writes_land(queue: JobQueue) -> None:
    repo = _repo_for(queue)

    job = queue.enqueue_scheduled("owner", "dev")
    claimed = queue.claim_next("worker-A")
    assert claimed is not None

    run_a = repo.create_run("pipeline", job.id, target="dev")
    # The owner's transition lands.
    assert queue.transition_to_running(job.id, run_a, expected_worker_id="worker-A") is True
    running = queue.get_job(job.id)
    assert running is not None and running.status == "running" and running.run_id == run_a

    # The owner's finalize lands.
    assert queue.mark_finished(job.id, "succeeded", expected_worker_id="worker-A") is True
    finished = queue.get_job(job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.finished_at is not None


def test_unguarded_mark_finished_is_backward_compatible(queue: JobQueue) -> None:
    # expected_worker_id=None: writes unconditionally (the shipped behavior),
    # returns True, and raises KeyError on a missing job.
    job = queue.enqueue_scheduled("compat", "dev")
    queue.claim_next("worker-A")
    assert queue.mark_finished(job.id, "succeeded") is True
    back = queue.get_job(job.id)
    assert back is not None and back.status == "succeeded"

    with pytest.raises(KeyError):
        queue.mark_finished("job_does_not_exist", "failed")


def test_unguarded_transition_to_running_is_backward_compatible(queue: JobQueue) -> None:
    repo = _repo_for(queue)
    job = queue.enqueue_scheduled("compat2", "dev")
    queue.claim_next("worker-A")
    run = repo.create_run("pipeline", job.id, target="dev")
    # Unguarded transition still works and returns True.
    assert queue.transition_to_running(job.id, run) is True
    back = queue.get_job(job.id)
    assert back is not None and back.status == "running"

    with pytest.raises(KeyError):
        queue.transition_to_running("job_does_not_exist", run)
