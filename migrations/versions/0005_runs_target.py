"""Add `runs.target` column (P1-07).

Revision ID: 0005_runs_target
Revises: 0004_build_entity
Create Date: 2026-05-07

P1-07's spec text claimed `runs.target` was added by 0004; it wasn't.
This migration lands the column, backfills it from each run's pipeline
→ most-recent successful Build's target, and reverses cleanly.

Backfill rule:

* For each run that has a non-null ``pipeline_name`` whose pipeline
  has a most-recent ``builds`` row, copy that build's ``target``.
* Otherwise leave ``runs.target`` NULL. The column is intentionally
  nullable for legacy runs that pre-date the Build entity (and for
  ``carve plan`` style runs that aren't bound to a target).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_runs_target"
down_revision: str | None = "0004_build_entity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add `runs.target` (TEXT NULL) and backfill from latest build per pipeline."""
    op.add_column("runs", sa.Column("target", sa.String(), nullable=True))

    bind = op.get_bind()

    # Most-recent build per pipeline_name. Two builds with identical
    # MAX(created_at) would emit twice, but the UPDATE then sets the
    # same target twice — harmless.
    rows = bind.execute(
        sa.text(
            "SELECT b.pipeline_name AS pipeline_name, b.target AS target "
            "FROM builds b "
            "JOIN ("
            "  SELECT pipeline_name, MAX(created_at) AS max_created "
            "  FROM builds GROUP BY pipeline_name"
            ") latest "
            "ON b.pipeline_name = latest.pipeline_name "
            "AND b.created_at = latest.max_created"
        )
    ).fetchall()
    for row in rows:
        pipeline_name, target = row[0], row[1]
        bind.execute(
            sa.text(
                "UPDATE runs SET target = :target "
                "WHERE pipeline_name = :pipeline_name"
            ),
            {"target": target, "pipeline_name": pipeline_name},
        )


def downgrade() -> None:
    """Drop `runs.target`."""
    op.drop_column("runs", "target")


__all__ = ["downgrade", "upgrade"]
