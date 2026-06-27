"""Add the runtime queue tables: jobs, workers, step_runs.

Revision ID: 0008_runtime_queue
Revises: 0007_workspaces
Create Date: 2026-06-26

The Increment-4 runtime turns a queued unit of work into a persisted run.
This migration lands the three tables that carry it:

* ``jobs`` — the durable work queue. The two **partial unique indexes**
  (``ix_jobs_one_queued_per_pipeline`` ``WHERE status='queued'`` and
  ``ix_jobs_one_running_per_pipeline`` ``WHERE status='running'``) are the
  load-bearing invariant: at most one queued and one running job per
  ``(pipeline, tenant_id)``, enforced by Postgres so a racing enqueue/claim
  cannot break it. ``ix_jobs_claim_order`` backs the ``FOR UPDATE SKIP
  LOCKED`` claim's ordering; ``ix_jobs_heartbeat_at`` supports the (deferred)
  reaper's stale-claim scan.
* ``workers`` — one row per worker process draining the queue.
* ``step_runs`` — the per-step persistence the real ``StepSink`` writes (see
  the runtime delivery spec's discrepancy note: the spec claimed this was
  carried forward from M1, but the tree had only ``runs``/``logs`` — so this
  slice CREATES it).

The scheduler/heartbeat/reaper/archiver loops and their tables
(``schedules``/``schedule_changes``/``events``/``*_archive``) are deferred to
later runtime slices. Downgrade drops the three tables, restoring 0007's
schema exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0008_runtime_queue"
down_revision: str | None = "0007_workspaces"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``jobs``, ``workers``, ``step_runs`` + their indexes."""
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        # ``pipeline`` is a plain string, NOT a FK to ``pipelines.name``: a
        # manual/api trigger may enqueue a pipeline that exists on disk
        # (``pipelines/<name>.toml``) before a ``pipelines`` row is written by
        # a build, so coupling enqueue to that row would reject a valid job.
        sa.Column("pipeline", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("trigger", sa.String(), nullable=False, server_default="manual"),
        sa.Column("scheduled_for", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "run_id",
            sa.String(),
            sa.ForeignKey("runs.id", name="fk_jobs_run_id"),
            nullable=True,
        ),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    # The two load-bearing partial unique indexes: at most one queued and one
    # running job per (pipeline, tenant_id). These are the ON CONFLICT targets
    # and the PipelineAlreadyRunning enforcer.
    op.create_index(
        "ix_jobs_one_queued_per_pipeline",
        "jobs",
        ["pipeline", "tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_jobs_one_running_per_pipeline",
        "jobs",
        ["pipeline", "tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    # The claim ordering index: claim_next selects the oldest-due queued job.
    op.create_index(
        "ix_jobs_claim_order",
        "jobs",
        ["tenant_id", "scheduled_for", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index("ix_jobs_heartbeat_at", "jobs", ["heartbeat_at"])
    op.create_index("ix_jobs_status_created_at", "jobs", ["status", "created_at"])

    op.create_table(
        "workers",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    op.create_table(
        "step_runs",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(),
            sa.ForeignKey("runs.id", name="fk_step_runs_run_id"),
            nullable=False,
        ),
        sa.Column("step_id", sa.String(), nullable=False),
        sa.Column("step_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "outputs",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_step_runs_run_id", "step_runs", ["run_id"])
    op.create_index(
        "ix_step_runs_run_id_step_id_attempt",
        "step_runs",
        ["run_id", "step_id", "attempt"],
    )


def downgrade() -> None:
    """Drop ``step_runs``, ``workers``, ``jobs`` (and their indexes)."""
    op.drop_index("ix_step_runs_run_id_step_id_attempt", table_name="step_runs")
    op.drop_index("ix_step_runs_run_id", table_name="step_runs")
    op.drop_table("step_runs")

    op.drop_table("workers")

    op.drop_index("ix_jobs_status_created_at", table_name="jobs")
    op.drop_index("ix_jobs_heartbeat_at", table_name="jobs")
    op.drop_index("ix_jobs_claim_order", table_name="jobs")
    op.drop_index("ix_jobs_one_running_per_pipeline", table_name="jobs")
    op.drop_index("ix_jobs_one_queued_per_pipeline", table_name="jobs")
    op.drop_table("jobs")


__all__ = ["downgrade", "upgrade"]
