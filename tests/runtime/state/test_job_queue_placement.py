"""Worker-placement at the queue layer: the label-filtered claim + the stamp.

Postgres-fixture-gated (the claim rides ``FOR UPDATE SKIP LOCKED`` + the partial
claim index, neither of which exists in SQLite). Two things are proven here:

* **The claim label matrix.** ``claim_next(worker_label=...)`` filters the queue
  by the added predicate ``AND (required_label IS NULL OR required_label =
  :worker_label)``. A labeled worker claims matching-labeled + unlabeled jobs; an
  **unlabeled worker claims only unlabeled jobs** (the SQL-NULL semantics: for a
  labeled job ``required_label = NULL`` is never true, so only the ``IS NULL``
  branch matches — intended, asserted, not papered over); a labeled job left for a
  non-matching worker **stays queued** until a matching worker arrives.
* **The stamp.** ``enqueue_scheduled``/``enqueue_manual`` persist the passed
  ``required_label`` (and the manual upsert refreshes it via ``EXCLUDED``); the
  default ``None`` writes a NULL column (the flat-pool back-compat).
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
from carve.core.state.job_queue import JobQueue


@pytest.fixture
def queue(postgres_state_store_url: str) -> JobQueue:
    config = Config(
        project=ProjectConfig(name="placement-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return JobQueue(create_session_factory(engine))


# --- the stamp ----------------------------------------------------------------


def test_enqueue_scheduled_stamps_required_label(queue: JobQueue) -> None:
    job = queue.enqueue_scheduled("sales", "dev", required_label="onprem-dbt")
    assert job.required_label == "onprem-dbt"


def test_enqueue_scheduled_defaults_required_label_to_null(queue: JobQueue) -> None:
    job = queue.enqueue_scheduled("sales", "dev")
    assert job.required_label is None


def test_enqueue_manual_stamps_required_label(queue: JobQueue) -> None:
    job = queue.enqueue_manual("sales", "dev", trigger="manual", required_label="onprem-dbt")
    assert job.required_label == "onprem-dbt"


def test_enqueue_manual_upsert_refreshes_required_label_via_excluded(queue: JobQueue) -> None:
    # The first enqueue stamps 'a'; the upsert onto the same queued row refreshes
    # it to 'b' (DO UPDATE SET required_label = EXCLUDED.required_label).
    first = queue.enqueue_manual("sales", "dev", trigger="manual", required_label="a")
    second = queue.enqueue_manual("sales", "prod", trigger="manual", required_label="b")
    assert second.id == first.id  # coalesced onto the one queued row
    assert second.required_label == "b"


def test_enqueue_manual_upsert_can_clear_required_label(queue: JobQueue) -> None:
    # EXCLUDED semantics also clear: a re-trigger with no label nulls the column.
    first = queue.enqueue_manual("sales", "dev", trigger="manual", required_label="a")
    second = queue.enqueue_manual("sales", "dev", trigger="manual")
    assert second.id == first.id
    assert second.required_label is None


# --- the claim label matrix ---------------------------------------------------


def test_labeled_worker_claims_matching_labeled_job(queue: JobQueue) -> None:
    queue.enqueue_scheduled("secure", "dev", required_label="onprem-dbt")
    claimed = queue.claim_next("w1", worker_label="onprem-dbt")
    assert claimed is not None
    assert claimed.pipeline == "secure"
    assert claimed.status == "claimed"


def test_labeled_worker_also_claims_unlabeled_jobs(queue: JobQueue) -> None:
    queue.enqueue_scheduled("open", "dev")  # required_label IS NULL
    claimed = queue.claim_next("w1", worker_label="onprem-dbt")
    assert claimed is not None
    assert claimed.pipeline == "open"


def test_labeled_worker_does_not_claim_a_differently_labeled_job(queue: JobQueue) -> None:
    queue.enqueue_scheduled("secure", "dev", required_label="onprem-dbt")
    # A worker advertising a DIFFERENT label can't claim it.
    assert queue.claim_next("w1", worker_label="near-source") is None
    # The job is untouched, still queued for its matching worker.
    matched = queue.claim_next("w2", worker_label="onprem-dbt")
    assert matched is not None
    assert matched.pipeline == "secure"


def test_unlabeled_worker_does_not_claim_a_labeled_job(queue: JobQueue) -> None:
    # The SQL-NULL semantics, asserted: worker_label=NULL never matches a labeled
    # job (required_label = NULL is never true), so only the IS NULL branch can
    # match — an unlabeled worker sees a labeled-only queue as empty.
    job = queue.enqueue_scheduled("secure", "dev", required_label="onprem-dbt")
    assert queue.claim_next("w1") is None  # worker_label defaults to None
    # The labeled job stays queued (not claimed, not lost).
    still = queue.get_job(job.id)
    assert still is not None
    assert still.status == "queued"
    assert still.claimed_by is None


def test_unlabeled_worker_claims_the_unlabeled_job_and_skips_the_labeled_one(
    queue: JobQueue,
) -> None:
    # A labeled job enqueued FIRST (older) sits beside an unlabeled one. An
    # unlabeled worker skips the labeled job — even though it is oldest-due — and
    # claims the unlabeled one; the labeled job stays queued for a matching worker.
    labeled = queue.enqueue_scheduled("secure", "dev", required_label="onprem-dbt")
    queue.enqueue_scheduled("open", "dev")

    claimed = queue.claim_next("w1")  # unlabeled worker
    assert claimed is not None
    assert claimed.pipeline == "open"

    still_labeled = queue.get_job(labeled.id)
    assert still_labeled is not None
    assert still_labeled.status == "queued"


def test_labeled_job_left_queued_is_later_claimed_by_a_matching_worker(queue: JobQueue) -> None:
    job = queue.enqueue_scheduled("secure", "dev", required_label="onprem-dbt")
    # No unlabeled / non-matching worker can take it ...
    assert queue.claim_next("w-unlabeled") is None
    assert queue.claim_next("w-other", worker_label="near-source") is None
    # ... it waits, still queued, until a matching worker drains it.
    claimed = queue.claim_next("w-onprem", worker_label="onprem-dbt")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.claimed_by == "w-onprem"
