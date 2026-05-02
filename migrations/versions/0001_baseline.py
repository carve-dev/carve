"""Baseline schema — runs, logs, plans.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-04-29

Captures the M1 schema as-is, before the M1.1-06 pipeline-centric changes.
The shape mirrors `carve.core.state.models` at the time M1.1-05 shipped.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the M1 baseline tables.

    Idempotency note: ``carve init`` previously called
    ``Base.metadata.create_all()``. Existing dev DBs will already have
    these tables. We use `op.create_table` regardless — Alembic stamps
    revision rows in `alembic_version` once a migration is run, and on
    a brand-new DB the tables don't exist. For the upgrade-an-existing-
    DB case the project's bootstrap stamps the baseline before running
    `upgrade head`, so this migration is only ever invoked on a fresh
    DB.
    """
    op.create_table(
        "runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("tokens_input", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_output", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(),
            sa.ForeignKey("runs.id"),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_logs_run_id_timestamp",
        "logs",
        ["run_id", "timestamp"],
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("parent_plan_id", sa.String(), nullable=True),
        sa.Column("goal", sa.String(), nullable=False),
        sa.Column("config_hash", sa.String(), nullable=False),
        sa.Column("carve_version", sa.String(), nullable=False),
        sa.Column("estimates_json", sa.String(), nullable=False),
        sa.Column("task_graph_json", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column(
            "apply_run_id",
            sa.String(),
            sa.ForeignKey("runs.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("plans")
    op.drop_index("ix_logs_run_id_timestamp", table_name="logs")
    op.drop_table("logs")
    op.drop_table("runs")
