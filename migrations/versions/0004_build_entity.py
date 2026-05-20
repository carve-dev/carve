"""Build entity, per-target binding (P1-02).

Revision ID: 0004_build_entity
Revises: 0003_rename_apply_to_deploy
Create Date: 2026-05-07

Introduces the ``builds`` table — the deployable artifact produced by
``carve build`` — and rewires the Pipeline FK to point at it. Plan loses
three vestigial columns whose state Build now owns:

* ``plans.estimates_json`` — cost / duration / Snowflake-credit
  estimates dropped per the accepted M2-01 review.
* ``plans.deployed_at`` / ``plans.deploy_run_id`` — these were renamed
  to ``deploy*`` in 0003 but actually held the *first build* timestamp,
  not a real deploy. ``Build.created_at`` carries the same information
  now; the Build row's ``plan_id`` reverse lookup replaces the run id.

Migration env disables SQLite FK enforcement for the duration; the
runtime listener re-enables on every fresh connection.

Step order (must hold for backfill correctness):

1. Create ``builds`` table.
2. Backfill one Build row per existing pipeline whose
   ``current_plan_id`` is non-null.
3. Add ``pipelines.current_build_id`` (nullable FK to ``builds.id``)
   and stamp it from the synthesized backfill rows.
4. Drop ``pipelines.current_plan_id``.
5. Drop ``plans.estimates_json`` / ``deployed_at`` / ``deploy_run_id``.

Downgrade reverses in inverse order: re-add the dropped Plan columns,
re-add ``pipelines.current_plan_id``, repopulate it from the most-recent
build per pipeline, drop ``current_build_id``, drop ``builds``.
"""

from __future__ import annotations

import logging
import re
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# Mirror of carve.core.targets.registry.TARGET_NAME_RE — kept inline so
# the migration stays import-safe if the registry module is later moved.
_TARGET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# revision identifiers, used by Alembic.
revision: str = "0004_build_entity"
down_revision: str | None = "0003_rename_apply_to_deploy"
branch_labels: str | None = None
depends_on: str | None = None

_logger = logging.getLogger("alembic.0004_build_entity")


def upgrade() -> None:
    """Land the Build table and prune Plan's deploy-state columns.

    v0.1-01 retargeted this migration at Postgres:

    * ``manifest_json`` is now ``JSONB`` (was TEXT).
    * Timestamps use ``TIMESTAMP WITH TIME ZONE``.
    * ``batch_alter_table`` blocks are gone — Postgres handles ALTER
      TABLE natively without the rewrite-on-copy that SQLite required.
    """
    # 1. Create the `builds` table. Indices created here alongside the
    # table so the backfill in step 2 already has the
    # (pipeline_name, target, created_at) lookup index in place.
    op.create_table(
        "builds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "pipeline_name",
            sa.String(),
            sa.ForeignKey("pipelines.name", name="fk_builds_pipeline_name"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            sa.String(),
            sa.ForeignKey("plans.id", name="fk_builds_plan_id"),
            nullable=False,
        ),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "manifest_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{\"files\": []}'::jsonb"),
        ),
        sa.Column("commit_sha", sa.String(), nullable=True),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("deployed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_builds_pipeline_target_created_at",
        "builds",
        ["pipeline_name", "target", sa.text("created_at DESC")],
    )

    # 2. Backfill one Build per existing Pipeline whose current_plan_id
    # is non-null. The synthesized rows carry an empty manifest, which
    # makes deploy fail loudly until the user rebuilds — acceptable
    # because the backfilled rows correspond to dev-built pipelines that
    # have not been deployed.
    pipeline_to_build_id = _backfill_builds_from_pipelines()

    # 3. Add current_build_id and stamp it from the backfill map. Drop
    # current_plan_id once stamping completes.
    op.add_column(
        "pipelines",
        sa.Column(
            "current_build_id",
            sa.String(),
            sa.ForeignKey("builds.id", name="fk_pipelines_current_build_id"),
            nullable=True,
        ),
    )

    if pipeline_to_build_id:
        bind = op.get_bind()
        for pipeline_name, build_id in pipeline_to_build_id.items():
            bind.execute(
                sa.text(
                    "UPDATE pipelines SET current_build_id = :build_id "
                    "WHERE name = :name"
                ),
                {"build_id": build_id, "name": pipeline_name},
            )

    op.drop_column("pipelines", "current_plan_id")

    # 4. Drop the three vestigial Plan columns.
    op.drop_column("plans", "estimates_json")
    op.drop_column("plans", "deployed_at")
    op.drop_column("plans", "deploy_run_id")


def _backfill_builds_from_pipelines() -> dict[str, str]:
    """Synthesize Build rows from existing pipelines; return name→id map.

    For each ``pipelines`` row whose ``current_plan_id`` is non-null,
    insert one Build row. Returns a mapping from pipeline name to the
    synthesized build id, used by step 3 to stamp
    ``pipelines.current_build_id`` without a follow-up subquery.

    ``target`` defaults to the project's ``default_target`` if a
    ``carve.toml`` is reachable; otherwise falls back to ``"dev"``.

    ``created_at`` precedence:

    1. The plan's ``deployed_at`` (the M1.1-06 ``first build`` proxy).
    2. The pipeline's ``updated_at``.
    3. ``utcnow()`` as last resort.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT p.name AS pipeline_name, "
            "       p.current_plan_id AS plan_id, "
            "       p.updated_at AS pipeline_updated_at, "
            "       pl.deployed_at AS plan_deployed_at "
            "FROM pipelines p "
            "LEFT JOIN plans pl ON pl.id = p.current_plan_id "
            "WHERE p.current_plan_id IS NOT NULL"
        )
    ).fetchall()
    if not rows:
        return {}

    default_target = _read_default_target()
    now_ts = datetime.now(UTC)
    pipeline_to_build_id: dict[str, str] = {}

    for row in rows:
        pipeline_name = row[0]
        plan_id = row[1]
        pipeline_updated_at = row[2]
        plan_deployed_at = row[3]

        if isinstance(plan_deployed_at, datetime):
            created_at = plan_deployed_at
        elif isinstance(pipeline_updated_at, datetime):
            created_at = pipeline_updated_at
        else:
            created_at = now_ts

        build_id = "build_" + uuid.uuid4().hex
        # ``manifest_json`` is JSONB — cast the string literal so psycopg
        # accepts it without a parameter-type mismatch.
        bind.execute(
            sa.text(
                "INSERT INTO builds "
                "(id, pipeline_name, plan_id, target, created_at, "
                " manifest_json, commit_sha, pr_url, deployed_at) "
                "VALUES "
                "(:id, :pipeline_name, :plan_id, :target, :created_at, "
                " CAST(:manifest_json AS jsonb), NULL, NULL, NULL)"
            ),
            {
                "id": build_id,
                "pipeline_name": pipeline_name,
                "plan_id": plan_id,
                "target": default_target,
                "created_at": created_at,
                "manifest_json": '{"files": []}',
            },
        )
        pipeline_to_build_id[pipeline_name] = build_id

    return pipeline_to_build_id


def _read_default_target() -> str:
    """Best-effort lookup of ``[project].default_target`` from ``carve.toml``.

    The migration runs from the project root via the Carve runtime, so
    ``Path.cwd() / "carve.toml"`` is the file we want. Any failure
    (missing file, malformed TOML, missing field) falls back to ``"dev"``
    silently — the migration must succeed against a brand-new database
    where no ``carve.toml`` is on disk yet (alembic CLI invocation from
    a repo maintainer's checkout).
    """
    candidate = Path.cwd() / "carve.toml"
    if not candidate.is_file():
        return "dev"
    try:
        data = tomllib.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        _logger.info("could not read carve.toml for default_target: %s", exc)
        return "dev"
    project = data.get("project")
    if not isinstance(project, dict):
        return "dev"
    target = project.get("default_target")
    if isinstance(target, str) and target:
        # Defense in depth: a hand-edited `default_target = "../escape"`
        # would otherwise flow into Build rows and from there into deploy
        # paths. Reject and fall back to "dev" rather than poisoning the
        # backfill with a malformed value.
        if not _TARGET_NAME_RE.fullmatch(target):
            _logger.warning(
                "ignoring malformed default_target=%r in carve.toml; "
                "using 'dev' for backfill",
                target,
            )
            return "dev"
        return target
    return "dev"


def downgrade() -> None:
    """Reverse the upgrade in inverse order."""
    # 1. Restore the three Plan columns.
    op.add_column(
        "plans",
        sa.Column(
            "estimates_json",
            sa.String(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "plans",
        sa.Column("deployed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "plans",
        sa.Column(
            "deploy_run_id",
            sa.String(),
            sa.ForeignKey("runs.id", name="fk_plans_deploy_run_id"),
            nullable=True,
        ),
    )

    # 2. Restore pipelines.current_plan_id (nullable FK to plans.id).
    op.add_column(
        "pipelines",
        sa.Column(
            "current_plan_id",
            sa.String(),
            sa.ForeignKey("plans.id", name="fk_pipelines_current_plan_id"),
            nullable=True,
        ),
    )

    # 3. Repopulate current_plan_id from the most recent Build per pipeline.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT b.pipeline_name, b.plan_id "
            "FROM builds b "
            "JOIN ("
            "  SELECT pipeline_name, MAX(created_at) AS max_created "
            "  FROM builds GROUP BY pipeline_name"
            ") latest "
            "ON b.pipeline_name = latest.pipeline_name "
            "AND b.created_at = latest.max_created"
        )
    ).fetchall()
    seen: set[str] = set()
    for row in rows:
        pipeline_name, plan_id = row[0], row[1]
        if pipeline_name in seen:
            # Two builds with identical max(created_at); the JOIN above
            # may emit both. Keep the first.
            continue
        seen.add(pipeline_name)
        bind.execute(
            sa.text(
                "UPDATE pipelines SET current_plan_id = :plan_id "
                "WHERE name = :name"
            ),
            {"plan_id": plan_id, "name": pipeline_name},
        )

    # 4. Drop pipelines.current_build_id.
    op.drop_column("pipelines", "current_build_id")

    # 5. Drop the builds table (and its index).
    op.drop_index(
        "ix_builds_pipeline_target_created_at",
        table_name="builds",
    )
    op.drop_table("builds")


__all__ = ["downgrade", "upgrade"]
