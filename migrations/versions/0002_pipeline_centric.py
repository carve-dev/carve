"""Pipeline-centric lifecycle (M1.1-06).

Revision ID: 0002_pipeline_centric
Revises: 0001_baseline
Create Date: 2026-04-29

Adds the `pipelines` table, the new ``phase`` and ``pipeline_name``
columns on `plans`, and ``pipeline_name`` on `runs`. Backfills a
synthesized `Pipeline` row for any pre-existing plan with
``applied_at IS NOT NULL`` (best-effort — JSON shape may not match).

v0.1-01 retargeted the migration at Postgres: timestamps use
``TIMESTAMP WITH TIME ZONE``, the ``task_graph_json`` column the backfill
reads is JSONB so it deserialises to ``dict`` directly, and the
``batch_alter_table`` blocks compile down to plain ALTER on Postgres.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# Allowed shape for synthesized `pipeline_name`. Must match
# `carve.cli.orchestrator.planner._PIPELINE_NAME_RE` — the runtime uses
# the same pattern. Validating here protects against malformed
# `task_graph_json.pipeline_dir` payloads in legacy databases: an
# unvalidated trailing path component could otherwise inject path
# traversal characters, whitespace, or arbitrary unicode into a freshly
# created Pipeline row, where downstream code (the runner, the CLI)
# would then trust it as a directory name.
_PIPELINE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# revision identifiers, used by Alembic.
revision: str = "0002_pipeline_centric"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None

_logger = logging.getLogger("alembic.0002_pipeline_centric")


def upgrade() -> None:
    """Add pipelines table, new columns, and backfill from prior data."""
    # 1. New `pipelines` table.
    op.create_table(
        "pipelines",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("pipeline_dir", sa.String(), nullable=False),
        sa.Column(
            "current_plan_id",
            sa.String(),
            sa.ForeignKey("plans.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "last_run_id",
            sa.String(),
            sa.ForeignKey("runs.id"),
            nullable=True,
        ),
        sa.Column("last_run_status", sa.String(), nullable=True),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # 2. New columns on `plans`. Postgres supports plain ALTER TABLE
    # ADD COLUMN; we no longer need ``batch_alter_table``. Both columns
    # are nullable (phase has a server_default so existing rows take
    # ``'drafted'``) and the CHECK constraint enforces the phase enum.
    op.add_column(
        "plans",
        sa.Column(
            "phase",
            sa.String(),
            nullable=False,
            server_default="drafted",
        ),
    )
    op.add_column(
        "plans",
        sa.Column(
            "pipeline_name",
            sa.String(),
            sa.ForeignKey(
                "pipelines.name",
                name="fk_plans_pipeline_name",
            ),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_plans_phase",
        "plans",
        "phase IN ('drafted', 'built')",
    )

    # 3. New column on `runs`.
    op.add_column(
        "runs",
        sa.Column(
            "pipeline_name",
            sa.String(),
            sa.ForeignKey(
                "pipelines.name",
                name="fk_runs_pipeline_name",
            ),
            nullable=True,
        ),
    )

    # 4. Backfill from previously-applied plans.
    _backfill_pipelines_from_applied_plans()


def _backfill_pipelines_from_applied_plans() -> None:
    """Best-effort backfill of `pipelines` rows for prior applied plans.

    Walks the `plans` table looking for rows with `applied_at IS NOT NULL`
    and parses each row's `task_graph_json` for the directory name. When
    that's available, the plan is marked `phase='built'`, its
    `pipeline_name` is set, and a synthesized `Pipeline` row is inserted.

    Failures are logged and skipped — this migration must succeed against
    a brand-new database where the `plans` table is empty as well as a
    smoke-tested dev database whose JSON shape may already drift.
    """
    bind = op.get_bind()
    # Use raw SQL via the bind so this works against any backend Alembic
    # supports without taking a dependency on the ORM.
    plans = bind.execute(
        sa.text(
            "SELECT id, task_graph_json, applied_at, apply_run_id, created_at "
            "FROM plans WHERE applied_at IS NOT NULL"
        )
    ).fetchall()
    if not plans:
        return

    seen_pipelines: set[str] = set()
    for row in plans:
        plan_id: str = row[0]
        task_graph_raw = row[1]
        applied_at = row[2]
        apply_run_id = row[3]
        created_at = row[4]
        # ``task_graph_json`` is JSONB on Postgres so psycopg returns a
        # dict already; defensively json.loads for the legacy TEXT path
        # an offline-migrated database might still expose.
        if isinstance(task_graph_raw, dict):
            task_graph = task_graph_raw
        else:
            try:
                task_graph = json.loads(task_graph_raw or "{}")
            except (TypeError, ValueError) as exc:
                _logger.info(
                    "skipping backfill for plan %s: malformed task_graph_json (%s)",
                    plan_id,
                    exc,
                )
                continue

        pipeline_dir = task_graph.get("pipeline_dir")
        if not isinstance(pipeline_dir, str) or not pipeline_dir:
            _logger.info(
                "skipping backfill for plan %s: pipeline_dir missing from task_graph",
                plan_id,
            )
            continue

        # Derive pipeline name from the trailing directory component.
        # e.g. "pipelines/iowa_liquor_sales" -> "iowa_liquor_sales".
        pipeline_name = pipeline_dir.rstrip("/").rsplit("/", 1)[-1]
        if not pipeline_name:
            continue
        # Reject anything that isn't snake_case ASCII. A malformed
        # `pipeline_dir` (path traversal, whitespace, unicode, etc.)
        # would otherwise be inserted as a real Pipeline row that the
        # runner is forced to trust as a directory name.
        if not _PIPELINE_NAME_RE.match(pipeline_name):
            _logger.info(
                "skipping backfill for plan %s: derived pipeline_name %r "
                "is not valid snake_case",
                plan_id,
                pipeline_name,
            )
            continue

        # Timestamps are TIMESTAMPTZ now; values from `bind.execute` come
        # back as aware datetimes on Postgres. Fall back to an aware UTC
        # `now()` when the source row's timestamp is missing.
        now_ts = (
            applied_at
            if isinstance(applied_at, datetime)
            else datetime.now(UTC)
        )
        first_seen = (
            created_at
            if isinstance(created_at, datetime)
            else now_ts
        )

        if pipeline_name not in seen_pipelines:
            bind.execute(
                sa.text(
                    "INSERT INTO pipelines "
                    "(name, description, pipeline_dir, current_plan_id, "
                    "created_at, updated_at, last_run_id, last_run_status, "
                    "last_run_at) VALUES "
                    "(:name, :description, :pipeline_dir, :current_plan_id, "
                    ":created_at, :updated_at, :last_run_id, NULL, NULL)"
                ),
                {
                    "name": pipeline_name,
                    "description": "",
                    "pipeline_dir": pipeline_dir,
                    "current_plan_id": plan_id,
                    "created_at": first_seen,
                    "updated_at": now_ts,
                    "last_run_id": apply_run_id,
                },
            )
            seen_pipelines.add(pipeline_name)

        # Mark the plan built and link to its pipeline.
        bind.execute(
            sa.text(
                "UPDATE plans SET phase = 'built', pipeline_name = :name "
                "WHERE id = :id"
            ),
            {"name": pipeline_name, "id": plan_id},
        )


def downgrade() -> None:
    op.drop_column("runs", "pipeline_name")
    op.drop_constraint("ck_plans_phase", "plans", type_="check")
    op.drop_column("plans", "pipeline_name")
    op.drop_column("plans", "phase")
    op.drop_table("pipelines")
