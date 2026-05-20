"""Rename `apply` -> `deploy` on plans and runs.

Revision ID: 0003_rename_apply_to_deploy
Revises: 0002_pipeline_centric
Create Date: 2026-05-04

The CLI verb `apply` was renamed to `deploy` (the M2 prod-PR-deploy
verb), and the schema follows: ``plans.applied_at`` -> ``plans.deployed_at``,
``plans.apply_run_id`` -> ``plans.deploy_run_id``. Any ``runs.kind = 'apply'``
rows are also rewritten to ``'deploy'`` defensively — the M1 stub never
inserted apply-kind runs, but legacy DBs that smoke-tested the
short-lived M1 applier may still carry such rows.

There is no CHECK constraint on ``runs.kind`` to update.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_rename_apply_to_deploy"
down_revision: str | None = "0002_pipeline_centric"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Rename plans columns and rewrite any apply-kind run rows."""
    # 1. Rename the two `plans` columns. Postgres supports plain
    # ``ALTER TABLE ... RENAME COLUMN`` so we no longer need
    # ``batch_alter_table``.
    op.alter_column("plans", "applied_at", new_column_name="deployed_at")
    op.alter_column("plans", "apply_run_id", new_column_name="deploy_run_id")

    # 2. Defensive rewrite: any historical run rows with kind='apply'
    # become kind='deploy'. Expected to be zero rows in practice.
    op.execute(
        sa.text("UPDATE runs SET kind = 'deploy' WHERE kind = 'apply'")
    )


def downgrade() -> None:
    """Reverse the column renames and restore apply-kind run rows."""
    op.execute(
        sa.text("UPDATE runs SET kind = 'apply' WHERE kind = 'deploy'")
    )
    op.alter_column("plans", "deploy_run_id", new_column_name="apply_run_id")
    op.alter_column("plans", "deployed_at", new_column_name="applied_at")
