"""Add the `workspaces` diagnostics table (control-plane layout).

Revision ID: 0007_workspaces
Revises: 0006_recovery_chains
Create Date: 2026-06-18

The control-plane layout introduces ``separate-remote`` components, each
cloned into ``<root>/.carve/workspaces/<name>/`` by the workspace cache.
This migration lands the diagnostics table the runtime updates after each
sync and the static UI queries:

* ``workspaces`` — one row per cached repo: ``name`` (PK, the derived
  cache-dir name), ``url``, ``branch``, ``last_synced_commit``,
  ``last_synced_at`` (TIMESTAMPTZ), and ``status`` constrained to
  ``clean`` / ``dirty`` / ``unreachable`` by ``ck_workspaces_status``.

Diagnostics only — the on-disk clone is the source of truth for code.
Downgrade drops the table, restoring 0006's schema exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_workspaces"
down_revision: str | None = "0006_recovery_chains"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the ``workspaces`` table with the status CHECK constraint."""
    op.create_table(
        "workspaces",
        sa.Column("name", sa.String(), primary_key=True, nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("branch", sa.String(), nullable=True),
        sa.Column("last_synced_commit", sa.String(), nullable=True),
        sa.Column(
            "last_synced_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="clean",
        ),
        sa.CheckConstraint(
            "status IN ('clean', 'dirty', 'unreachable')",
            name="ck_workspaces_status",
        ),
    )


def downgrade() -> None:
    """Drop the ``workspaces`` table."""
    op.drop_table("workspaces")


__all__ = ["downgrade", "upgrade"]
