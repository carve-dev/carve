"""Add `runs.parent_run_id` for recovery-attempt chains (P1-09).

Revision ID: 0006_recovery_chains
Revises: 0005_runs_target
Create Date: 2026-05-07

P1-09 introduces a recovery agent that retries failed runs by spawning
child Run rows. This migration lands the column the chain hangs off of:

* ``runs.parent_run_id`` — TEXT NULL, FK to ``runs.id``. Set on a
  recovery-attempt run; NULL for original runs and for runs that
  pre-date P1-09.
* ``ix_runs_parent_run_id`` — index on the new column. The lookup
  pattern is ``WHERE parent_run_id = <id>`` to walk the chain in
  ``carve runs <id> --recovery``.

Existing rows get NULL on upgrade. Downgrade drops the index then the
column, restoring 0005's schema exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_recovery_chains"
down_revision: str | None = "0005_runs_target"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``runs.parent_run_id`` (nullable, FK to runs.id) and its index."""
    with op.batch_alter_table("runs") as batch:
        batch.add_column(
            sa.Column(
                "parent_run_id",
                sa.String(),
                sa.ForeignKey("runs.id", name="fk_runs_parent_run_id"),
                nullable=True,
            )
        )
    op.create_index(
        "ix_runs_parent_run_id",
        "runs",
        ["parent_run_id"],
    )


def downgrade() -> None:
    """Drop the index and the column."""
    op.drop_index("ix_runs_parent_run_id", table_name="runs")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("parent_run_id")


__all__ = ["downgrade", "upgrade"]
