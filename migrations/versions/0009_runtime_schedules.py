"""Add the scheduler tables: schedules, schedule_changes.

Revision ID: 0009_runtime_schedules
Revises: 0008_runtime_queue
Create Date: 2026-06-26

The Increment-4 runtime's **scheduler slice** treats the ``schedules`` table as
the source of truth: a row fires its pipeline onto the ``jobs`` queue at each
cron tick (``carve serve``), and ``carve schedule pause/resume/set-cron`` mutates
the live row instantly with a ``schedule_changes`` audit trail.

* ``schedules`` — one live schedule per ``(pipeline, tenant_id)``
  (``ix_schedules_one_per_pipeline``). ``ix_schedules_due`` is a **partial**
  index on ``next_fires_at WHERE paused = false`` — the due-query rides it. The
  ``ck_schedules_pause_origin`` CHECK structurally enforces "pause origin is set
  iff paused, and is one of ('user', 'recovery')", so the later recovery slice's
  auto-pause lands against a complete column (the columns + CHECK ship now; the
  recovery *mutators* are deferred).
* ``schedule_changes`` — the append-only audit trail; one row per live mutation
  (``before``/``after`` JSONB snapshots, nullable ``actor_token_id``).

The ``[seed_schedule]`` reconciler-seed + ``carve schedule reseed`` (PIPELINES),
the full ``events`` table/emitter, the reaper/archiver, and the ``carve serve``
multi-loop supervisor are deferred to later slices. Downgrade drops both tables,
restoring 0008's schema exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0009_runtime_schedules"
down_revision: str | None = "0008_runtime_queue"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``schedules`` + ``schedule_changes`` with their indexes + CHECK."""
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        # ``pipeline`` is a plain string, NOT a FK to ``pipelines.name`` — a
        # schedule may be seeded for a pipeline that exists on disk before its
        # ``pipelines`` row is written by a build (same rationale as
        # ``jobs.pipeline``).
        sa.Column("pipeline", sa.String(), nullable=False),
        sa.Column("cron", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("paused_by", sa.String(), nullable=True),
        sa.Column("pause_reason", sa.String(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=False, server_default="UTC"),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_fired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_fires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        # Pause origin is set iff paused, and is 'user' or 'recovery'. The
        # ``paused_by IS NOT NULL`` guard is load-bearing: a bare
        # ``paused_by IN (...)`` yields SQL NULL for a NULL origin, and a CHECK
        # that evaluates to NULL PASSES (Postgres only rejects explicit
        # ``false``). The guard forces the paused branch to ``false`` so a
        # ``paused=true, paused_by=NULL`` row is rejected.
        sa.CheckConstraint(
            "(paused = false AND paused_by IS NULL) "
            "OR (paused = true AND paused_by IS NOT NULL "
            "AND paused_by IN ('user', 'recovery'))",
            name="ck_schedules_pause_origin",
        ),
    )
    # One live schedule per (pipeline, tenant_id).
    op.create_index(
        "ix_schedules_one_per_pipeline",
        "schedules",
        ["pipeline", "tenant_id"],
        unique=True,
    )
    # The due-query index: ``list_due`` selects unpaused rows whose next_fires_at
    # has passed. Partial (``WHERE paused = false``) so paused rows never enter
    # the scan — does not exist in SQLite, hence the Postgres-fixture gate.
    op.create_index(
        "ix_schedules_due",
        "schedules",
        ["next_fires_at"],
        postgresql_where=sa.text("paused = false"),
    )

    op.create_table(
        "schedule_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("pipeline", sa.String(), nullable=False),
        sa.Column("change_kind", sa.String(), nullable=False),
        sa.Column("before", JSONB, nullable=True),
        sa.Column("after", JSONB, nullable=True),
        sa.Column("actor_token_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="cli"),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("changed_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_schedule_changes_pipeline_changed_at",
        "schedule_changes",
        ["pipeline", "changed_at"],
    )


def downgrade() -> None:
    """Drop ``schedule_changes`` and ``schedules`` (and their indexes)."""
    op.drop_index("ix_schedule_changes_pipeline_changed_at", table_name="schedule_changes")
    op.drop_table("schedule_changes")

    op.drop_index("ix_schedules_due", table_name="schedules")
    op.drop_index("ix_schedules_one_per_pipeline", table_name="schedules")
    op.drop_table("schedules")


__all__ = ["downgrade", "upgrade"]
