"""Add the durable event log: events.

Revision ID: 0010_runtime_events
Revises: 0009_runtime_schedules
Create Date: 2026-06-29

The Increment-4 runtime's **events slice** turns the already-shipped no-op
``_emit`` seams (``schedules._emit`` / ``job_queue._emit`` / the persisting
sink's ``step.*``) durable: every in-scope state transition writes one row into
``events`` via ``src/carve/runtime/events.py``'s :class:`EventEmitter`.

* ``events`` — the append-only event log. ``id`` is ``BIGSERIAL`` (mirrors
  ``schedule_changes``/``logs`` — append-only, DB-generated, NOT the
  app-generated ``String`` ids of the entity tables); ``payload`` is ``JSONB
  NOT NULL`` (the emitter always supplies it). ``ix_events_unprocessed`` is a
  **partial** index on ``occurred_at WHERE processed_at IS NULL`` — the seam a
  future webhook/relay (spec 09) scans to find undelivered events, kept cheap
  by excluding already-processed rows.

REST/MCP/webhook *delivery* of events, the archiver + ``*_archive`` tables, and
``schedule.reseeded``/``archive.batch_completed`` (their emitters) stay deferred
to later slices. Downgrade drops the index then the table, restoring 0009's
schema exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0010_runtime_events"
down_revision: str | None = "0009_runtime_schedules"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``events`` + the partial ``ix_events_unprocessed`` index."""
    op.create_table(
        "events",
        # BIGSERIAL: an append-only log id, DB-generated — mirrors
        # ``schedule_changes``/``logs``, not the app-generated String entity ids.
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        # NOT NULL: the emitter always supplies a payload (``{}`` at minimum).
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "occurred_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
    )
    # The unprocessed-scan index: a relay/webhook (spec 09) finds undelivered
    # events by ``processed_at IS NULL ORDER BY occurred_at``. Partial (only
    # unprocessed rows enter the index) so it stays small as the log grows —
    # Postgres-only, like ``ix_schedules_due``.
    op.create_index(
        "ix_events_unprocessed",
        "events",
        ["occurred_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )


def downgrade() -> None:
    """Drop ``events`` (and its partial index)."""
    op.drop_index("ix_events_unprocessed", table_name="events")
    op.drop_table("events")


__all__ = ["downgrade", "upgrade"]
