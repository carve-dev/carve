"""The job-queue repository — enqueue, the safe concurrent claim, transitions.

This is the runtime's durable work queue, kept in Postgres precisely for two
concurrency primitives that application code cannot get right on its own:

1. **Enqueue dedup is a partial unique index, not check-then-insert.**
   ``enqueue_scheduled`` issues ``INSERT ... ON CONFLICT DO NOTHING`` against
   ``ix_jobs_one_queued_per_pipeline`` (``WHERE status='queued'``); a racing
   second enqueue inserts zero rows and we fail safe with
   :class:`QueuedJobAlreadyExists`. There is never a window where two queued
   jobs for one pipeline coexist.
2. **The claim is ``FOR UPDATE SKIP LOCKED``.** ``claim_next`` runs the spec's
   exact ``UPDATE ... WHERE id = (SELECT id ... FOR UPDATE SKIP LOCKED LIMIT 1)
   RETURNING *`` (raw SQL — SKIP LOCKED has no ORM query-API expression). Two
   workers racing one queued job: one wins; the other's subquery skips the
   locked row and matches nothing (returns ``None``), never blocking and never
   double-claiming.

The state store is **synchronous** SQLAlchemy: these methods are plain sync
transactions. The async ``execute_pipeline``/``StepSink`` call them via
``asyncio.to_thread`` so DB I/O never blocks the event loop (no async engine
this slice). The capability spec's ``async def claim_next`` is conceptual.

Kept separate from ``repository.py`` to bound the diff; constructed from the
same ``sessionmaker`` the :class:`~carve.core.state.repository.Repository` uses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from carve.core.state.models import Job, StepRun, Worker

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


class QueuedJobAlreadyExists(RuntimeError):
    """Raised when a pipeline already has a queued job and one more is enqueued.

    The partial unique index ``ix_jobs_one_queued_per_pipeline`` is the
    enforcer — ``enqueue_scheduled`` treats "ON CONFLICT inserted 0 rows" as
    this error rather than ever double-queueing.
    """


class PipelineAlreadyRunning(RuntimeError):
    """Raised when a job is promoted to ``running`` but the pipeline already has one.

    The partial unique index ``ix_jobs_one_running_per_pipeline`` serializes
    runs of a single pipeline; ``transition_to_running`` surfaces its violation
    as this error so the worker can release the claim cleanly.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobQueue:
    """Typed access to the ``jobs``/``workers``/``step_runs`` queue tables.

    Construct once per process from the same ``sessionmaker`` as
    :class:`~carve.core.state.repository.Repository`. Every method opens a
    short sync transaction and returns detached ORM objects (or plain values);
    ``expire_on_commit=False`` on the factory keeps returned rows readable.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ----------------------------------------------------------------- Enqueue

    def enqueue_scheduled(
        self,
        pipeline: str,
        target: str,
        *,
        scheduled_for: datetime | None = None,
        tenant_id: int = 1,
    ) -> Job:
        """Enqueue a scheduled job, deduped by the one-queued partial index.

        Uses ``INSERT ... ON CONFLICT (pipeline, tenant_id) WHERE
        status='queued' DO NOTHING RETURNING id`` — the conflict target infers
        the partial unique index. If the insert returns no row, a queued job
        for this pipeline already exists and we raise
        :class:`QueuedJobAlreadyExists`. **Never** check-then-insert: that
        races and double-queues under concurrency.
        """
        job_id = "job_" + uuid.uuid4().hex
        now = _utcnow()
        stmt = sa.text(
            """
            INSERT INTO jobs
                (id, pipeline, target, status, trigger, scheduled_for,
                 tenant_id, created_at)
            VALUES
                (:id, :pipeline, :target, 'queued', 'scheduled', :scheduled_for,
                 :tenant_id, :created_at)
            ON CONFLICT (pipeline, tenant_id) WHERE status = 'queued'
            DO NOTHING
            RETURNING id
            """
        )
        with self._session_factory() as session:
            inserted_id = session.execute(
                stmt,
                {
                    "id": job_id,
                    "pipeline": pipeline,
                    "target": target,
                    "scheduled_for": scheduled_for,
                    "tenant_id": tenant_id,
                    "created_at": now,
                },
            ).scalar_one_or_none()
            if inserted_id is None:
                session.rollback()
                raise QueuedJobAlreadyExists(
                    f"pipeline {pipeline!r} already has a queued job (tenant {tenant_id})"
                )
            session.commit()
            job = session.get(Job, inserted_id)
            assert job is not None  # just inserted in this transaction
            return job

    def enqueue_manual(
        self,
        pipeline: str,
        target: str,
        *,
        trigger: str = "manual",
        tenant_id: int = 1,
    ) -> Job:
        """Enqueue a manual/api job, upserting onto the existing queued row.

        Unlike ``enqueue_scheduled`` (which rejects a duplicate), a manual
        trigger is idempotent: ``INSERT ... ON CONFLICT ... DO UPDATE`` refreshes
        the existing queued row's ``trigger``/``target`` and returns the **same**
        ``id``, so a user mashing "run now" coalesces onto one queued job rather
        than erroring. ``scheduled_for`` is cleared (a manual job runs ASAP).
        """
        job_id = "job_" + uuid.uuid4().hex
        now = _utcnow()
        stmt = sa.text(
            """
            INSERT INTO jobs
                (id, pipeline, target, status, trigger, scheduled_for,
                 tenant_id, created_at)
            VALUES
                (:id, :pipeline, :target, 'queued', :trigger, NULL,
                 :tenant_id, :created_at)
            ON CONFLICT (pipeline, tenant_id) WHERE status = 'queued'
            DO UPDATE SET trigger = EXCLUDED.trigger,
                          target = EXCLUDED.target,
                          scheduled_for = NULL
            RETURNING id
            """
        )
        with self._session_factory() as session:
            returned_id = session.execute(
                stmt,
                {
                    "id": job_id,
                    "pipeline": pipeline,
                    "target": target,
                    "trigger": trigger,
                    "tenant_id": tenant_id,
                    "created_at": now,
                },
            ).scalar_one()
            session.commit()
            job = session.get(Job, returned_id)
            assert job is not None
            return job

    # ------------------------------------------------------------------- Claim

    def claim_next(self, worker_id: str, *, tenant_id: int = 1) -> Job | None:
        """Atomically claim the oldest-due queued job for ``worker_id``.

        The spec's exact ``FOR UPDATE SKIP LOCKED`` claim: a single ``UPDATE``
        whose ``WHERE id = (SELECT id ... ORDER BY scheduled_for ASC NULLS LAST,
        created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED)`` row-locks the chosen
        queued job and skips any a concurrent worker has already locked. The
        winner flips it to ``claimed``, stamping ``claimed_by``/``claimed_at``
        and the single ``heartbeat_at`` for this slice. Returns the claimed
        :class:`Job`, or ``None`` if nothing was claimable (empty queue, or
        every queued row already locked by a peer).

        Raw SQL via ``session.execute(text(...))`` — SKIP LOCKED has no ORM
        query-API expression. The subquery + the ``UPDATE`` run in one
        statement, so the lock is held only for the row flip.
        """
        now = _utcnow()
        stmt = sa.text(
            """
            UPDATE jobs
            SET status = 'claimed',
                claimed_by = :worker_id,
                claimed_at = :now,
                heartbeat_at = :now
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'queued'
                  AND tenant_id = :tenant_id
                  AND (scheduled_for IS NULL OR scheduled_for <= :now)
                ORDER BY scheduled_for ASC NULLS LAST, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id
            """
        )
        with self._session_factory() as session:
            claimed_id = session.execute(
                stmt,
                {"worker_id": worker_id, "now": now, "tenant_id": tenant_id},
            ).scalar_one_or_none()
            if claimed_id is None:
                session.rollback()
                return None
            session.commit()
            return session.get(Job, claimed_id)

    # ------------------------------------------------------------- Transitions

    def transition_to_running(self, job_id: str, run_id: str) -> None:
        """Promote a ``claimed`` job to ``running``, binding it to its ``runs`` row.

        Re-checks the one-running-per-pipeline invariant: the move to
        ``status='running'`` collides with ``ix_jobs_one_running_per_pipeline``
        if another job for the same pipeline is already running, which surfaces
        as :class:`PipelineAlreadyRunning`. The worker releases its claim on
        that error rather than running a second concurrent execution.
        """
        now = _utcnow()
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(f"job {job_id!r} not found")
            job.status = "running"
            job.run_id = run_id
            job.started_at = now
            try:
                session.commit()
            except sa.exc.IntegrityError as exc:
                session.rollback()
                raise PipelineAlreadyRunning(
                    f"pipeline {job.pipeline!r} already has a running job"
                ) from exc

    def mark_finished(
        self,
        job_id: str,
        status: str,
        *,
        error_message: str | None = None,
    ) -> None:
        """Move a job to a terminal state (``succeeded``/``failed``/...).

        Stamps ``finished_at`` and records ``error_message`` verbatim. Terminal
        statuses leave the partial running/queued indexes so the pipeline is
        immediately eligible to be enqueued/claimed again.
        """
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(f"job {job_id!r} not found")
            job.status = status
            job.finished_at = _utcnow()
            if error_message is not None:
                job.error_message = error_message
            session.commit()

    def release_claim(self, job_id: str) -> None:
        """Return a ``claimed`` job to ``queued`` (e.g. on PipelineAlreadyRunning).

        Clears ``claimed_by``/``claimed_at`` so the next claimer treats it as a
        fresh queued job. Idempotent for a job that is already queued.
        """
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(f"job {job_id!r} not found")
            job.status = "queued"
            job.claimed_by = None
            job.claimed_at = None
            session.commit()

    def update_heartbeat(self, job_id: str, *, now: datetime | None = None) -> None:
        """Stamp a job's ``heartbeat_at`` (the column ships; the loop is deferred)."""
        stamp = now if now is not None else _utcnow()
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(f"job {job_id!r} not found")
            job.heartbeat_at = stamp
            session.commit()

    def get_job(self, job_id: str) -> Job | None:
        """Fetch a job by id, or ``None`` if not found."""
        with self._session_factory() as session:
            return session.get(Job, job_id)

    # ----------------------------------------------------------------- Workers

    def register_worker(
        self,
        worker_id: str,
        *,
        host: str,
        pid: int,
        label: str | None = None,
        tenant_id: int = 1,
    ) -> Worker:
        """Insert (or refresh) the ``workers`` row for a worker process.

        Idempotent on ``id`` so a worker that restarts under the same id (rare
        — the id carries a startup uuid) refreshes its row rather than erroring.
        """
        now = _utcnow()
        with self._session_factory() as session:
            worker = session.get(Worker, worker_id)
            if worker is None:
                worker = Worker(
                    id=worker_id,
                    host=host,
                    pid=pid,
                    label=label,
                    tenant_id=tenant_id,
                    status="active",
                    started_at=now,
                    last_heartbeat_at=now,
                )
                session.add(worker)
            else:
                worker.host = host
                worker.pid = pid
                worker.label = label
                worker.status = "active"
                worker.last_heartbeat_at = now
            session.commit()
            session.refresh(worker)
            return worker

    def unregister_worker(self, worker_id: str) -> None:
        """Mark a worker ``stopped`` at clean shutdown (no-op if absent)."""
        with self._session_factory() as session:
            worker = session.get(Worker, worker_id)
            if worker is None:
                return
            worker.status = "stopped"
            worker.last_heartbeat_at = _utcnow()
            session.commit()

    def get_worker(self, worker_id: str) -> Worker | None:
        """Fetch a worker row by id, or ``None``."""
        with self._session_factory() as session:
            return session.get(Worker, worker_id)

    # --------------------------------------------------------------- Step runs

    def create_step_run(
        self,
        *,
        run_id: str,
        step_id: str,
        step_type: str,
        attempt: int,
        started_at: datetime | None = None,
    ) -> str:
        """Insert a ``running`` ``step_runs`` row; return its id.

        Called by the persisting :class:`StepSink` at ``step_started``. The id
        is generated client-side so the sink can address the row at
        ``step_finished`` without a lookup by ``(run_id, step_id, attempt)``.
        """
        step_run_id = "steprun_" + uuid.uuid4().hex
        with self._session_factory() as session:
            session.add(
                StepRun(
                    id=step_run_id,
                    run_id=run_id,
                    step_id=step_id,
                    step_type=step_type,
                    status="running",
                    attempt=attempt,
                    started_at=started_at if started_at is not None else _utcnow(),
                )
            )
            session.commit()
        return step_run_id

    def finish_step_run(
        self,
        step_run_id: str,
        *,
        status: str,
        outputs: dict[str, Any] | None = None,
        error_message: str | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Transition a ``step_runs`` row to its terminal status.

        Called by the persisting :class:`StepSink` at ``step_finished``: writes
        the terminal ``status`` (``succeeded``/``failed``/``skipped``), the
        threaded ``outputs`` (JSONB), ``error_message``, ``finished_at``, and
        ``duration_ms``.
        """
        with self._session_factory() as session:
            step_run = session.get(StepRun, step_run_id)
            if step_run is None:
                raise KeyError(f"step_run {step_run_id!r} not found")
            step_run.status = status
            step_run.outputs = outputs if outputs is not None else {}
            step_run.error_message = error_message
            step_run.finished_at = finished_at if finished_at is not None else _utcnow()
            step_run.duration_ms = duration_ms
            session.commit()

    def list_step_runs(self, run_id: str) -> list[StepRun]:
        """Return a run's ``step_runs`` rows in insertion order (for tests/UI)."""
        stmt = sa.select(StepRun).where(StepRun.run_id == run_id).order_by(StepRun.created_at.asc())
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())


__all__ = [
    "JobQueue",
    "PipelineAlreadyRunning",
    "QueuedJobAlreadyExists",
]
