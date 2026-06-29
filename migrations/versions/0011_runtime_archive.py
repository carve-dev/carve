"""Add the runtime archive tables: jobs_archive, runs_archive, logs_archive, step_runs_archive.

Revision ID: 0011_runtime_archive
Revises: 0010_runtime_events
Create Date: 2026-06-29

The Increment-4 runtime's **archiver slice** moves terminal rows older than each
table's retention window out of the live ``jobs``/``runs``/``logs``/``step_runs``
tables into ``*_archive`` siblings, keeping the active tables (and their hot
indexes) small. The archiver itself lives in ``src/carve/runtime/archiver.py``.

Each archive table is a **structural clone** of its active table created with
``CREATE TABLE <t>_archive (LIKE <t> INCLUDING ALL EXCLUDING INDEXES)`` — Alembic
has no ``LIKE`` helper, so the create is raw ``op.execute`` (the rest of the
migration mirrors its ``op.create_table`` siblings). ``INCLUDING ALL`` carries the
column types, NOT NULL, defaults, and CHECKs so an ``INSERT INTO <t>_archive
SELECT * FROM <t>`` round-trips column-for-column; ``EXCLUDING INDEXES`` drops the
copied PRIMARY KEY/UNIQUE indexes (an append-only history table needs no PK), and
``LIKE`` never copies FOREIGN KEYs — so the archive tables stand alone and a
``runs`` delete is never blocked by an archived child row.

Then four **access-pattern indexes** back the expected history queries:

* ``ix_jobs_archive_pipeline_finished_at`` on ``(pipeline, finished_at DESC)``
* ``ix_runs_archive_pipeline_finished_at`` on ``(pipeline_name, completed_at
  DESC)`` — the live ``runs`` table has ``pipeline_name``/``completed_at`` (NOT
  the ``pipeline``/``finished_at`` of the deferred-block sketch); the documented
  index *name* is kept.
* ``ix_logs_archive_run_id_timestamp`` on ``(run_id, timestamp)``
* ``ix_step_runs_archive_run_id`` on ``(run_id)``

Downgrade drops the four indexes then the four tables, restoring 0010's schema
exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_runtime_archive"
down_revision: str | None = "0010_runtime_events"
branch_labels: str | None = None
depends_on: str | None = None

# The four live tables that get an archive sibling, in no particular order here
# (the archiver enforces FK-safe delete ordering at runtime, not the DDL).
_ARCHIVED_TABLES = ("jobs", "runs", "logs", "step_runs")


def upgrade() -> None:
    """Create the four ``*_archive`` clone tables + their access-pattern indexes."""
    for table in _ARCHIVED_TABLES:
        # LIKE INCLUDING ALL EXCLUDING INDEXES: same column shape + defaults +
        # CHECKs, no copied PK/unique indexes, and (always) no copied FKs.
        op.execute(f"CREATE TABLE {table}_archive (LIKE {table} INCLUDING ALL EXCLUDING INDEXES)")

    op.create_index(
        "ix_jobs_archive_pipeline_finished_at",
        "jobs_archive",
        ["pipeline", sa.text("finished_at DESC")],
    )
    op.create_index(
        "ix_runs_archive_pipeline_finished_at",
        "runs_archive",
        ["pipeline_name", sa.text("completed_at DESC")],
    )
    op.create_index(
        "ix_logs_archive_run_id_timestamp",
        "logs_archive",
        ["run_id", "timestamp"],
    )
    op.create_index(
        "ix_step_runs_archive_run_id",
        "step_runs_archive",
        ["run_id"],
    )


def downgrade() -> None:
    """Drop the four ``*_archive`` indexes + tables, restoring 0010's schema."""
    op.drop_index("ix_step_runs_archive_run_id", table_name="step_runs_archive")
    op.drop_index("ix_logs_archive_run_id_timestamp", table_name="logs_archive")
    op.drop_index("ix_runs_archive_pipeline_finished_at", table_name="runs_archive")
    op.drop_index("ix_jobs_archive_pipeline_finished_at", table_name="jobs_archive")
    for table in _ARCHIVED_TABLES:
        op.execute(f"DROP TABLE {table}_archive")


__all__ = ["downgrade", "upgrade"]
