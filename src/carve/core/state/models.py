"""SQLAlchemy 2.0 declarative models for the Carve state store.

Six tables: `runs`, `logs`, `plans`, `pipelines`, `builds`, and
`workspaces`. The schema is managed by Alembic — see ``migrations/`` —
but the ORM models are still the canonical Python representation that
repository methods return.

v0.1-01 ported the state store from SQLite to Postgres. Concretely:

* `Plan.task_graph_json` and `Build.manifest_json` are now JSONB columns.
  The ORM-side accessor returns a ``dict[str, Any]`` (psycopg's default
  for JSONB), so callers no longer ``json.loads`` the value.
* All timestamp columns are ``TIMESTAMP WITH TIME ZONE``. Defaults still
  produce UTC-aware datetimes; the column round-trips them as UTC.

The repository continues to be the single SQL-issuing module — these
classes are the typed shape of the rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Native Postgres types used by the models. ``TIMESTAMPTZ`` is the column
# type for every timestamp; ``JSONB`` for the three free-form JSON
# payloads. We keep the import tight so the rest of the file stays
# backend-agnostic looking.
_TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)


def _utcnow() -> datetime:
    """Aware UTC ``now()``, used as a default factory.

    v0.1-01 retired the naive-UTC convention from M1 — Postgres handles
    ``TIMESTAMPTZ`` natively. Callers reading rows back get aware
    datetimes; tests that previously stripped ``tzinfo`` should attach
    UTC instead.
    """
    return datetime.now(UTC)


def _default_plan_expiry() -> datetime:
    """Plans expire 24 hours after creation by default."""
    return _utcnow() + timedelta(hours=24)


class Base(DeclarativeBase):
    """Declarative base for all state-store models."""


class Run(Base):
    """A single execution of a plan or pipeline."""

    __tablename__ = "runs"

    __table_args__ = (Index("ix_runs_parent_run_id", "parent_run_id"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str]
    target_id: Mapped[str]
    target: Mapped[str | None] = mapped_column(default=None)
    pipeline_name: Mapped[str | None] = mapped_column(
        ForeignKey("pipelines.name"),
        default=None,
    )
    # Set on recovery-attempt runs; NULL for the original failed run and
    # for everything created before P1-09. The chain is reachable via
    # ``SELECT * FROM runs WHERE parent_run_id = <run_id>``.
    parent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", name="fk_runs_parent_run_id"),
        default=None,
    )
    owner_user_id: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="queued")
    started_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    duration_ms: Mapped[int | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    tokens_input: Mapped[int] = mapped_column(default=0)
    tokens_output: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)


class Log(Base):
    """A single streamed log line attached to a run."""

    __tablename__ = "logs"
    __table_args__ = (Index("ix_logs_run_id_timestamp", "run_id", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    timestamp: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    level: Mapped[str]
    source: Mapped[str]
    message: Mapped[str]


class Plan(Base):
    """Index row for a plan stored on disk at ``.carve/plans/<id>.json``.

    Plan is now strictly a *design* artifact: it captures what the user
    asked for and what the planner agreed to build, but it carries no
    deploy/build state. P1-02 dropped ``estimates_json``, ``deployed_at``,
    and ``deploy_run_id`` — the corresponding state moved to the new
    `Build` table (which also owns the per-target binding).

    Two columns added in M1.1-06 remain:

    * ``phase`` — ``drafted`` (default) or ``built``. CHECK constraint
      enforces those two values; transitions are driven by the build
      flow, never written by hand.
    * ``pipeline_name`` — set during ``carve build`` to the name the
      build agent landed on. ``NULL`` while the plan is still in the
      drafted phase.

    v0.1-01 changed ``task_graph_json`` from TEXT to JSONB. The ORM
    surface returns ``dict[str, Any]`` directly — callers no longer
    parse the string themselves.
    """

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('drafted', 'built')",
            name="ck_plans_phase",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    parent_plan_id: Mapped[str | None] = mapped_column(default=None)
    goal: Mapped[str]
    config_hash: Mapped[str]
    carve_version: Mapped[str]
    task_graph_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    file_path: Mapped[str]
    phase: Mapped[str] = mapped_column(default="drafted")
    pipeline_name: Mapped[str | None] = mapped_column(
        ForeignKey("pipelines.name"),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_default_plan_expiry)


class Pipeline(Base):
    """A first-class pipeline asset.

    The directory under ``el/<name>/`` is the source of truth for code
    (P1.1-01 flattened the layout). This row exists so the CLI/UI can
    list pipelines, filter their runs, and walk plan/build lineage
    without re-reading every file.

    The ``last_run_*`` columns are denormalised from `runs` so that
    ``carve pipelines`` doesn't need a per-row JOIN. They are updated by
    `Repository.record_pipeline_run` when a run reaches a terminal state.

    P1-02 replaced ``current_plan_id`` with ``current_build_id`` — the
    deployable artifact is now Build, and Plan is reachable via
    ``Build.plan_id``.
    """

    __tablename__ = "pipelines"

    name: Mapped[str] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(default="")
    pipeline_dir: Mapped[str]
    current_build_id: Mapped[str | None] = mapped_column(
        ForeignKey("builds.id"),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    last_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id"),
        default=None,
    )
    last_run_status: Mapped[str | None] = mapped_column(default=None)
    last_run_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)


class Build(Base):
    """A deployable artifact produced by ``carve build``.

    Every successful build creates one row. The build binds a Plan
    (the *design*) to a target (``dev``, ``prod``, etc.) and a manifest
    of files written. ``carve el deploy`` consumes the manifest to ship
    the build to its target.

    Indexed on ``(pipeline_name, target, created_at DESC)`` so
    "latest build of <name> for <target>" stays a cheap lookup.

    v0.1-01 changed ``manifest_json`` from TEXT to JSONB; callers read
    a ``dict[str, Any]`` directly.
    """

    __tablename__ = "builds"
    __table_args__ = (
        Index(
            "ix_builds_pipeline_target_created_at",
            "pipeline_name",
            "target",
            sa.text("created_at DESC"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    pipeline_name: Mapped[str] = mapped_column(ForeignKey("pipelines.name"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    target: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=lambda: {"files": []},
    )
    commit_sha: Mapped[str | None] = mapped_column(default=None)
    pr_url: Mapped[str | None] = mapped_column(default=None)
    deployed_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)


class Workspace(Base):
    """Diagnostics row for a cached ``separate-remote`` component.

    The control plane clones each ``separate-remote`` component into
    ``<root>/.carve/workspaces/<name>/`` (see
    ``carve.integrations.workspace_cache``). This table records the sync
    result so the static UI can show, per cached repo, what revision it's
    on and whether it's healthy. It is **diagnostics only** — the source
    of truth for the code is the on-disk clone, not this row. The heavy
    querying is the UI's (Increment 5); the repository keeps a thin
    upsert/read surface.

    ``name`` is the workspace's derived cache-dir name (``slug(url)`` +
    branch/ref), which is unique per (url, revision) and stable across
    syncs — hence the primary key. ``status`` is constrained to the three
    documented values.
    """

    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('clean', 'dirty', 'unreachable')",
            name="ck_workspaces_status",
        ),
    )

    name: Mapped[str] = mapped_column(primary_key=True)
    url: Mapped[str]
    branch: Mapped[str | None] = mapped_column(default=None)
    last_synced_commit: Mapped[str | None] = mapped_column(default=None)
    last_synced_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    status: Mapped[str] = mapped_column(default="clean")


# ---------------------------------------------------------------------------
# Runtime queue: jobs, workers, step_runs (Increment 4 — queue+worker slice)
# ---------------------------------------------------------------------------
#
# The runtime turns a queued unit of work into a persisted run. Three tables
# carry it: ``jobs`` (the durable work queue), ``workers`` (the processes that
# drain it), and ``step_runs`` (the per-step persistence the ``StepSink`` writes
# — see the runtime delivery spec's discrepancy note: the spec claimed this was
# carried from M1, but the tree had only ``runs``/``logs``, so this slice
# CREATES it). The scheduler/heartbeat/reaper/archiver loops and their tables
# (``schedules``/``events``/``*_archive``) are deferred to later runtime slices.


class Job(Base):
    """A unit of work in the durable runtime queue.

    A job is enqueued (``status='queued'``), claimed by exactly one worker
    (``claimed``), promoted to ``running`` once the worker has created its
    ``runs`` row, then reaches a terminal state (``succeeded``/``failed``/
    ``cancelled``/``timed_out``). The two **partial unique indexes** are the
    load-bearing invariant: at most one ``queued`` and at most one ``running``
    job may exist per ``(pipeline, tenant_id)`` — enforced by Postgres, not
    application code, so a racing enqueue/claim cannot break it by accident.

    ``run_id`` is the FK to the ``runs`` row the worker creates at
    ``transition_to_running`` (NULL while queued/claimed). ``heartbeat_at`` is
    stamped once at claim in this slice; the heartbeat *loop* + reaper are
    deferred. ``trigger`` records what enqueued the job (``scheduled``/
    ``manual``/``api``); ``scheduled_for`` is the due time for a scheduled job
    (NULL for a manual one).
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # At most one queued job per pipeline+tenant. The ON CONFLICT target
        # for ``enqueue_scheduled``/``enqueue_manual`` — a racing second
        # enqueue fails safe rather than double-queueing.
        Index(
            "ix_jobs_one_queued_per_pipeline",
            "pipeline",
            "tenant_id",
            unique=True,
            postgresql_where=sa.text("status = 'queued'"),
        ),
        # At most one running job per pipeline+tenant. Backs
        # ``transition_to_running``'s ``PipelineAlreadyRunning`` serialization.
        Index(
            "ix_jobs_one_running_per_pipeline",
            "pipeline",
            "tenant_id",
            unique=True,
            postgresql_where=sa.text("status = 'running'"),
        ),
        # The claim ordering index: ``claim_next`` selects the oldest-due
        # queued job (``scheduled_for ASC NULLS LAST, created_at ASC``).
        Index(
            "ix_jobs_claim_order",
            "tenant_id",
            "scheduled_for",
            "created_at",
            postgresql_where=sa.text("status = 'queued'"),
        ),
        # Supports the (deferred) reaper's stale-claim scan.
        Index("ix_jobs_heartbeat_at", "heartbeat_at"),
        Index("ix_jobs_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    # ``pipeline`` is a plain string, NOT a FK to ``pipelines.name``: a
    # manual/api trigger may enqueue a pipeline that exists on disk before a
    # ``pipelines`` row is written by a build (see the migration comment).
    pipeline: Mapped[str]
    target: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued")
    trigger: Mapped[str] = mapped_column(default="manual")
    scheduled_for: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    tenant_id: Mapped[int] = mapped_column(default=1)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", name="fk_jobs_run_id"),
        default=None,
    )
    claimed_by: Mapped[str | None] = mapped_column(default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    started_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)


class Worker(Base):
    """A process that drains the job queue.

    The id is the spec's ``<hostname>:<pid>:<startup-uuid>`` — stable for the
    life of one worker process, unique across restarts. ``last_heartbeat_at``
    is written at registration (and, in later slices, by the heartbeat loop).
    ``status`` is ``active``/``stopped`` — set ``stopped`` at clean shutdown.
    """

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(primary_key=True)
    host: Mapped[str]
    pid: Mapped[int]
    label: Mapped[str | None] = mapped_column(default=None)
    tenant_id: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="active")
    started_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    last_heartbeat_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)


class StepRun(Base):
    """One step's execution, persisted by the runtime's ``StepSink``.

    The ``StepSink`` seam declared in ``execute_pipeline`` has waited for this
    table since Increment 3: ``step_started`` inserts a ``running`` row,
    ``step_finished`` transitions it to the step's terminal ``status``
    (``succeeded``/``failed``/``skipped``) with ``outputs`` (the threaded
    cross-step dict, JSONB), ``error_message``, ``finished_at``, and
    ``duration_ms``. ``run_id`` is the FK to the parent ``runs`` row;
    ``(run_id, step_id, attempt)`` identifies a row across retries.
    """

    __tablename__ = "step_runs"
    __table_args__ = (
        Index("ix_step_runs_run_id", "run_id"),
        Index("ix_step_runs_run_id_step_id_attempt", "run_id", "step_id", "attempt"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", name="fk_step_runs_run_id"))
    step_id: Mapped[str]
    step_type: Mapped[str]
    status: Mapped[str] = mapped_column(default="running")
    attempt: Mapped[int] = mapped_column(default=1)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error_message: Mapped[str | None] = mapped_column(default=None)
    started_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    duration_ms: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)


# ---------------------------------------------------------------------------
# Scheduler: schedules + schedule_changes (Increment 4 — scheduler slice)
# ---------------------------------------------------------------------------
#
# The scheduler treats ``schedules`` as the **source of truth** (not
# ``pipelines/<name>.toml``, not the reconciler). A row fires its pipeline onto
# the ``jobs`` queue at each cron tick; ``carve schedule pause/resume/set-cron``
# mutates the live row instantly, and every mutation appends a
# ``schedule_changes`` audit row. The ``[seed_schedule]`` reconciler-seed +
# ``carve schedule reseed`` (PIPELINES) and the full ``events`` table/emitter
# are deferred — this slice ships the live schedule + its audit trail only.


class Schedule(Base):
    """A live pipeline schedule — the scheduler's source of truth.

    One row per ``(pipeline, tenant_id)`` (the unique
    ``ix_schedules_one_per_pipeline``). ``next_fires_at`` is the load-bearing
    column: the scheduler's ``list_due`` selects ``paused = false AND
    next_fires_at <= now`` (riding the **partial** ``ix_schedules_due`` index,
    ``WHERE paused = false``), and every fire must **advance** ``next_fires_at``
    to the FOLLOWING cron tick in the same transaction — otherwise the row stays
    due and re-fires every loop tick (the dedup backstop would mask it, which is
    the wrong reason). ``cron`` is evaluated in ``timezone`` (DST-correct via
    croniter + zoneinfo); ``next_fires_at``/``last_fired_at`` are stored UTC-aware.

    ``paused`` gates firing; ``paused_by`` records the *origin* of a pause
    (``user`` via CLI/API, or ``recovery`` for the deferred auto-pause). The
    ``ck_schedules_pause_origin`` CHECK structurally enforces "origin is set iff
    paused, and is one of ('user', 'recovery')" so the later recovery slice's
    ``auto_pause_recovery`` lands against a complete, correct column.
    """

    __tablename__ = "schedules"
    __table_args__ = (
        # Pause origin is set iff the row is paused, and is one of the two
        # allowed origins. Recovery auto-pause (origin='recovery') ships as a
        # valid value now; the recovery *mutators* are deferred.
        # ``paused_by IS NOT NULL`` is load-bearing: a bare
        # ``paused_by IN (...)`` yields SQL NULL when ``paused_by`` is NULL, and
        # a CHECK that evaluates to NULL PASSES (Postgres only rejects an
        # explicit ``false``). The ``IS NOT NULL`` guard forces the paused branch
        # to ``false`` (not NULL) for a NULL origin, so the row is rejected.
        CheckConstraint(
            "(paused = false AND paused_by IS NULL) "
            "OR (paused = true AND paused_by IS NOT NULL "
            "AND paused_by IN ('user', 'recovery'))",
            name="ck_schedules_pause_origin",
        ),
        # One live schedule per pipeline+tenant.
        Index(
            "ix_schedules_one_per_pipeline",
            "pipeline",
            "tenant_id",
            unique=True,
        ),
        # The due-query index: ``list_due`` selects unpaused rows whose
        # next_fires_at has passed. Partial (``WHERE paused = false``) so paused
        # rows never enter the scan.
        Index(
            "ix_schedules_due",
            "next_fires_at",
            postgresql_where=sa.text("paused = false"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    # ``pipeline`` is a plain string, NOT a FK to ``pipelines.name`` — a schedule
    # may be seeded for a pipeline that exists on disk before its ``pipelines``
    # row is written by a build (same rationale as ``jobs.pipeline``).
    pipeline: Mapped[str]
    cron: Mapped[str]
    target: Mapped[str]
    paused: Mapped[bool] = mapped_column(default=False)
    paused_by: Mapped[str | None] = mapped_column(default=None)
    pause_reason: Mapped[str | None] = mapped_column(default=None)
    timezone: Mapped[str] = mapped_column(default="UTC")
    tenant_id: Mapped[int] = mapped_column(default=1)
    last_fired_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    next_fires_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)


class Event(Base):
    """One durable runtime event — the basis for webhooks/audit (spec 09).

    The events slice turns the runtime's no-op ``_emit`` seams durable: each
    in-scope state transition (``job.*``/``run.*``/``step.*``/``worker.*``/
    ``schedule.*``) writes one row here via
    :class:`~carve.runtime.events.EventEmitter`. The write is **best-effort**
    observability — an emit failure is logged and swallowed, never blocking the
    run/loop that triggered it.

    ``id`` is ``BIGSERIAL`` — an append-only DB-generated log id, mirroring
    :class:`ScheduleChange`/:class:`Log`, **not** the app-generated ``String``
    ids of the entity tables. ``payload`` is ``JSONB`` **NOT NULL** (no default:
    the emitter always supplies the taxonomy payload). ``processed_at`` is the
    delivery cursor a future relay/webhook stamps; the partial
    ``ix_events_unprocessed`` (``WHERE processed_at IS NULL``) keeps the
    undelivered-scan cheap.
    """

    __tablename__ = "events"
    __table_args__ = (
        # The unprocessed-scan index a relay/webhook (spec 09) rides. Partial
        # (``WHERE processed_at IS NULL``) so processed rows never enter it.
        Index(
            "ix_events_unprocessed",
            "occurred_at",
            postgresql_where=sa.text("processed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    tenant_id: Mapped[int] = mapped_column(default=1)


class ScheduleChange(Base):
    """One audited mutation of a ``schedules`` row (the live-change trail).

    Appended in the same transaction as the row mutation it records.
    ``change_kind`` is ``pause``/``resume``/``set_cron``/``reseed`` (``reseed``
    ships as a valid value for the deferred reconciler-seed); ``before``/``after``
    are JSONB snapshots of the changed fields. ``actor_token_id`` is the
    mutating auth token — **nullable** (NULL for the code seed and for the CLI
    until the auth slice fills it; ``source='cli'`` records the surface).
    """

    __tablename__ = "schedule_changes"
    __table_args__ = (
        Index(
            "ix_schedule_changes_pipeline_changed_at",
            "pipeline",
            "changed_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline: Mapped[str]
    change_kind: Mapped[str]
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    actor_token_id: Mapped[str | None] = mapped_column(default=None)
    source: Mapped[str] = mapped_column(default="cli")
    reason: Mapped[str | None] = mapped_column(default=None)
    tenant_id: Mapped[int] = mapped_column(default=1)
    changed_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)


__all__: list[Any] = [
    "Base",
    "Build",
    "Event",
    "Job",
    "Log",
    "Pipeline",
    "Plan",
    "Run",
    "Schedule",
    "ScheduleChange",
    "StepRun",
    "Worker",
    "Workspace",
]
