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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from carve.core.state.models import Job, StepRun, Worker

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    # Typed under TYPE_CHECKING only — a runtime ``from carve.runtime.events
    # import EventSink`` would re-enter the ``carve.runtime`` package mid-import
    # (its ``__init__`` eagerly pulls in the worker chain → this module). With
    # ``from __future__ import annotations`` the annotation is a string, so the
    # state store carries no runtime import of ``runtime.events``. The CLI
    # constructs the concrete ``EventEmitter`` and injects it here.
    from carve.runtime.events import EventSink


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

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        emitter: EventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        # The injected event sink (the concrete ``EventEmitter`` in production).
        # ``None`` ⇒ ``_emit`` stays a silent no-op, so every existing
        # caller/test that constructs ``JobQueue(factory)`` is unchanged.
        self._emitter = emitter

    # ----------------------------------------------------------------- Enqueue

    def enqueue_scheduled(
        self,
        pipeline: str,
        target: str,
        *,
        scheduled_for: datetime | None = None,
        required_label: str | None = None,
        tenant_id: int = 1,
    ) -> Job:
        """Enqueue a scheduled job, deduped by the one-queued partial index.

        Uses ``INSERT ... ON CONFLICT (pipeline, tenant_id) WHERE
        status='queued' DO NOTHING RETURNING id`` — the conflict target infers
        the partial unique index. If the insert returns no row, a queued job
        for this pipeline already exists and we raise
        :class:`QueuedJobAlreadyExists`. **Never** check-then-insert: that
        races and double-queues under concurrency.

        ``required_label`` is the worker-placement label the scheduler derived
        from the pipeline's referenced components (``None`` = any worker); it is
        stamped onto the row so ``claim_next`` can filter on it. Default ``None``
        keeps this back-compatible with every existing caller.
        """
        job_id = "job_" + uuid.uuid4().hex
        now = _utcnow()
        stmt = sa.text(
            """
            INSERT INTO jobs
                (id, pipeline, target, status, trigger, scheduled_for,
                 required_label, tenant_id, created_at)
            VALUES
                (:id, :pipeline, :target, 'queued', 'scheduled', :scheduled_for,
                 :required_label, :tenant_id, :created_at)
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
                    "required_label": required_label,
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
        self._emit(
            "job.queued",
            {
                "job_id": job.id,
                "pipeline": pipeline,
                "target": target,
                "trigger": "scheduled",
                "scheduled_for": scheduled_for.isoformat() if scheduled_for is not None else None,
            },
        )
        return job

    def enqueue_manual(
        self,
        pipeline: str,
        target: str,
        *,
        trigger: str = "manual",
        required_label: str | None = None,
        tenant_id: int = 1,
    ) -> Job:
        """Enqueue a manual/api job, upserting onto the existing queued row.

        Unlike ``enqueue_scheduled`` (which rejects a duplicate), a manual
        trigger is idempotent: ``INSERT ... ON CONFLICT ... DO UPDATE`` refreshes
        the existing queued row's ``trigger``/``target`` and returns the **same**
        ``id``, so a user mashing "run now" coalesces onto one queued job rather
        than erroring. ``scheduled_for`` is cleared (a manual job runs ASAP).

        ``required_label`` is the worker-placement label (``None`` = any worker);
        the upsert also refreshes it via ``EXCLUDED.required_label`` so a
        re-triggered row's label stays consistent with the current target/trigger.
        Default ``None`` keeps this back-compatible. ``enqueue_manual`` has no
        production caller yet (only tests) — the stamp is wired + repo-tested for
        forward-compat; the scheduler is the only live driver this slice.

        **Forward-compat contract (load-bearing when a live caller lands):** the
        upsert's ``required_label = EXCLUDED.required_label`` means a manual
        re-trigger with the default ``required_label=None`` will **clear** an
        already-labeled queued row's label. A live caller MUST therefore resolve
        and pass ``required_label`` (mirroring the scheduler's ``resolve_label``),
        or a coalescing "run now" onto a labeled scheduled job would unlabel it and
        let it run anywhere.
        """
        job_id = "job_" + uuid.uuid4().hex
        now = _utcnow()
        stmt = sa.text(
            """
            INSERT INTO jobs
                (id, pipeline, target, status, trigger, scheduled_for,
                 required_label, tenant_id, created_at)
            VALUES
                (:id, :pipeline, :target, 'queued', :trigger, NULL,
                 :required_label, :tenant_id, :created_at)
            ON CONFLICT (pipeline, tenant_id) WHERE status = 'queued'
            DO UPDATE SET trigger = EXCLUDED.trigger,
                          target = EXCLUDED.target,
                          scheduled_for = NULL,
                          required_label = EXCLUDED.required_label
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
                    "required_label": required_label,
                    "tenant_id": tenant_id,
                    "created_at": now,
                },
            ).scalar_one()
            session.commit()
            job = session.get(Job, returned_id)
            assert job is not None
        # A manual job is always ``scheduled_for=NULL`` (runs ASAP). The emit
        # rides the existing upsert transition — no new transition added.
        self._emit(
            "job.queued",
            {
                "job_id": job.id,
                "pipeline": pipeline,
                "target": target,
                "trigger": trigger,
                "scheduled_for": None,
            },
        )
        return job

    # ------------------------------------------------------------------- Claim

    def claim_next(
        self, worker_id: str, *, worker_label: str | None = None, tenant_id: int = 1
    ) -> Job | None:
        """Atomically claim the oldest-due queued job for ``worker_id``.

        The spec's exact ``FOR UPDATE SKIP LOCKED`` claim: a single ``UPDATE``
        whose ``WHERE id = (SELECT id ... ORDER BY scheduled_for ASC NULLS LAST,
        created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED)`` row-locks the chosen
        queued job and skips any a concurrent worker has already locked. The
        winner flips it to ``claimed``, stamping ``claimed_by``/``claimed_at``
        and the single ``heartbeat_at`` for this slice. Returns the claimed
        :class:`Job`, or ``None`` if nothing was claimable (empty queue, or
        every queued row already locked by a peer).

        **Worker placement.** ``worker_label`` is the label this worker
        advertises (``carve worker --label X``); the added predicate
        ``AND (required_label IS NULL OR required_label = :worker_label)`` filters
        the queue so a labeled job (``required_label = 'X'``) is claimed **only**
        by a matching worker, while unlabeled jobs (``required_label IS NULL``) run
        anywhere. The SQL-NULL semantics are load-bearing and intended: an
        **unlabeled** worker (``worker_label=NULL``) claims **only** unlabeled jobs
        — for a labeled job, ``required_label = NULL`` is never true, so only the
        ``IS NULL`` branch can match. ``worker_label=None`` (the default) makes the
        predicate a no-op for every unlabeled job, so every existing caller/test
        is byte-identical.

        Raw SQL via ``session.execute(text(...))`` — SKIP LOCKED has no ORM
        query-API expression, and ``:worker_label`` is a bound param (never
        interpolated). The subquery + the ``UPDATE`` run in one statement, so the
        lock is held only for the row flip.
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
                  AND (required_label IS NULL OR required_label = :worker_label)
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
                {
                    "worker_id": worker_id,
                    "worker_label": worker_label,
                    "now": now,
                    "tenant_id": tenant_id,
                },
            ).scalar_one_or_none()
            if claimed_id is None:
                session.rollback()
                return None
            session.commit()
            job = session.get(Job, claimed_id)
        if job is not None:
            self._emit("job.claimed", {"job_id": job.id, "worker_id": worker_id})
        return job

    # ------------------------------------------------------------- Transitions

    def transition_to_running(
        self,
        job_id: str,
        run_id: str,
        *,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Promote a ``claimed`` job to ``running``, binding it to its ``runs`` row.

        Re-checks the one-running-per-pipeline invariant: the move to
        ``status='running'`` collides with ``ix_jobs_one_running_per_pipeline``
        if another job for the same pipeline is already running, which surfaces
        as :class:`PipelineAlreadyRunning`. The worker releases its claim on that
        error rather than running a second concurrent execution.

        **The ownership guard.** When ``expected_worker_id`` is supplied, the
        flip is a guarded conditional ``UPDATE ... WHERE id=:job_id AND
        claimed_by=:worker_id AND status='claimed'`` — atomic, no read-then-write
        window. A worker that stalled past the reaper threshold, was reclaimed
        (its job returned to ``queued`` / re-claimed by a peer), and then returns
        matches **0 rows**: this is a **silent no-op** returning ``False`` (it
        lost the claim — the worker backs off, never stomps the new owner). When
        ``expected_worker_id is None`` the claim guard is skipped (existing
        callers/tests keep their behavior): a missing job then raises
        :class:`KeyError` as before.

        Returns ``True`` if the flip landed, ``False`` if the guard no-opped.
        """
        now = _utcnow()
        if expected_worker_id is not None:
            stmt = sa.text(
                """
                UPDATE jobs
                SET status = 'running', run_id = :run_id, started_at = :now
                WHERE id = :job_id
                  AND claimed_by = :worker_id
                  AND status = 'claimed'
                RETURNING id
                """
            )
            with self._session_factory() as session:
                try:
                    updated = session.execute(
                        stmt,
                        {
                            "job_id": job_id,
                            "run_id": run_id,
                            "now": now,
                            "worker_id": expected_worker_id,
                        },
                    ).scalar_one_or_none()
                except sa.exc.IntegrityError as exc:
                    session.rollback()
                    raise PipelineAlreadyRunning(
                        f"job {job_id!r} cannot run: pipeline already has a running job"
                    ) from exc
                session.commit()
                # 0 rows: the job was reclaimed/re-claimed away from this worker —
                # a returning zombie. Silent no-op; the worker treats it as a
                # lost claim and does NOT run.
                return updated is not None

        # Backward-compatible unguarded path (expected_worker_id is None): the
        # existing ORM-style flip, KeyError on a missing job.
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
            return True

    def mark_finished(
        self,
        job_id: str,
        status: str,
        *,
        error_message: str | None = None,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Move a job to a terminal state (``succeeded``/``failed``/...).

        Stamps ``finished_at`` and records ``error_message`` verbatim. Terminal
        statuses leave the partial running/queued indexes so the pipeline is
        immediately eligible to be enqueued/claimed again.

        **The ownership guard.** When ``expected_worker_id`` is supplied, the
        terminal write is a guarded conditional ``UPDATE ... WHERE id=:job_id AND
        claimed_by=:worker_id AND status IN ('claimed','running')`` — a single
        atomic statement. A worker that was reclaimed (stalled past the
        threshold) and returns to finalize matches **0 rows** (the job is now
        ``queued``/re-claimed by a peer, or already terminal): a **silent
        no-op** returning ``False`` so the zombie cannot double-finalize or stomp
        the new owner's state. When ``expected_worker_id is None`` the guard is
        skipped (existing callers/tests keep their unconditional behavior): a
        missing job raises :class:`KeyError` as before.

        Returns ``True`` if the terminal write landed, ``False`` if it no-opped.
        """
        if expected_worker_id is not None:
            stmt = sa.text(
                """
                UPDATE jobs
                SET status = :status,
                    finished_at = :now,
                    error_message = COALESCE(:error_message, error_message)
                WHERE id = :job_id
                  AND claimed_by = :worker_id
                  AND status IN ('claimed', 'running')
                RETURNING id
                """
            )
            with self._session_factory() as session:
                updated = session.execute(
                    stmt,
                    {
                        "job_id": job_id,
                        "status": status,
                        "now": _utcnow(),
                        "error_message": error_message,
                        "worker_id": expected_worker_id,
                    },
                ).scalar_one_or_none()
                session.commit()
                return updated is not None

        # Backward-compatible unguarded path (expected_worker_id is None).
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(f"job {job_id!r} not found")
            job.status = status
            job.finished_at = _utcnow()
            if error_message is not None:
                job.error_message = error_message
            session.commit()
            return True

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

    def update_heartbeat(
        self, job_id: str, *, now: datetime | None = None, expected_worker_id: str | None = None
    ) -> None:
        """Stamp a job's ``heartbeat_at``.

        ``expected_worker_id`` makes the beat ownership-aware (uniform with the
        worker's other writes): if the job is no longer claimed by this worker —
        e.g. a returning zombie whose job the reaper reclaimed and another worker
        re-claimed — the beat is a silent no-op rather than refreshing a job we no
        longer own. ``None`` stamps unconditionally (back-compat).
        """
        stamp = now if now is not None else _utcnow()
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(f"job {job_id!r} not found")
            if expected_worker_id is not None and job.claimed_by != expected_worker_id:
                return
            job.heartbeat_at = stamp
            session.commit()

    def reclaim_stale(
        self,
        now: datetime,
        *,
        stale_threshold_s: float = 60.0,
        tenant_id: int = 1,
    ) -> list[tuple[str, str | None, str | None]]:
        """Atomically reclaim every job whose heartbeat has gone stale.

        ONE statement — a ``WITH stale AS (SELECT ... FOR UPDATE SKIP LOCKED)
        UPDATE jobs SET status='queued', claimed_by=NULL, claimed_at=NULL,
        heartbeat_at=NULL FROM stale WHERE jobs.id = stale.id RETURNING jobs.id,
        jobs.run_id, stale.claimed_by``. Two reapers racing the same stale set
        cannot double-reclaim: the ``FOR UPDATE SKIP LOCKED`` snapshot makes each
        reaper lock a disjoint subset (the loser skips the locked rows), and once
        a row is flipped to ``queued`` it no longer matches ``status IN
        ('claimed','running')`` — so each stale job is reclaimed by exactly one
        reaper.

        Returns ``(id, run_id, prior_claimed_by)`` per reclaimed job: ``run_id``
        lets the reaper fail the orphaned in-flight Run; ``prior_claimed_by`` is
        the worker that *was* holding the claim (snapshotted in the CTE — the
        post-``UPDATE`` ``claimed_by`` is already NULL, so it must be captured
        before the flip) for the ``job.reclaimed`` audit event.

        **The cutoff is computed in Python** (``now - stale_threshold_s``) and
        passed as the BOUND param ``:cutoff`` — never string-interpolated into an
        ``INTERVAL`` literal (that would be injection-shaped). This mirrors
        ``claim_next``'s bound-param style.

        A reclaimed job goes back to ``queued`` with its claim + heartbeat
        cleared, so it is immediately re-claimable and the next worker re-runs it
        from scratch (step-level state is discarded). **``run_id`` is RETURNED
        (so the reaper can fail the orphaned in-flight Run) but is NOT nulled on
        the job** — it stays for audit; the next worker's
        ``transition_to_running`` overwrites it on re-claim.
        """
        cutoff = now - timedelta(seconds=stale_threshold_s)
        # ``RETURNING`` reflects the POST-update row, where ``claimed_by`` is now
        # NULL — so to return the *prior* owner (for the reaper's audit/event) we
        # snapshot it in a CTE that reads + locks the stale rows first, then UPDATE
        # ... FROM that snapshot and return the snapshot's ``claimed_by``. The
        # ``FOR UPDATE`` keeps the read-then-write atomic against a concurrent
        # reaper (the second reaper's snapshot CTE finds 0 rows once the first has
        # flipped them to ``queued`` — so still no double-reclaim).
        stmt = sa.text(
            """
            WITH stale AS (
                SELECT id, run_id, claimed_by
                FROM jobs
                WHERE status IN ('claimed', 'running')
                  AND tenant_id = :tenant_id
                  AND heartbeat_at < :cutoff
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs
            SET status = 'queued',
                claimed_by = NULL,
                claimed_at = NULL,
                heartbeat_at = NULL
            FROM stale
            WHERE jobs.id = stale.id
            RETURNING jobs.id, jobs.run_id, stale.claimed_by
            """
        )
        with self._session_factory() as session:
            rows = session.execute(
                stmt,
                {"tenant_id": tenant_id, "cutoff": cutoff},
            ).all()
            session.commit()
        return [(row.id, row.run_id, row.claimed_by) for row in rows]

    def get_job(self, job_id: str) -> Job | None:
        """Fetch a job by id, or ``None`` if not found."""
        with self._session_factory() as session:
            return session.get(Job, job_id)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        """The event-emit seam — delegates to the injected :class:`EventSink`.

        Every queue transition (``job.queued``/``job.claimed``/``job.reclaimed``,
        ``worker.registered``/``worker.unregistered``) and the persisting sink's
        ``step.*`` ride this one method; the reaper drives ``job.reclaimed``
        through it too. With **no** emitter injected it is a silent no-op — the
        back-compat path every pre-events caller/test keeps. With one injected it
        writes a durable ``events`` row (best-effort; the emitter swallows its own
        failures). Tests may still patch/spy it directly.
        """
        if self._emitter is not None:
            self._emitter.emit(kind, payload)

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
        self._emit("worker.registered", {"worker_id": worker_id, "host": host, "pid": pid})
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
            host, pid = worker.host, worker.pid
        self._emit("worker.unregistered", {"worker_id": worker_id, "host": host, "pid": pid})

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
