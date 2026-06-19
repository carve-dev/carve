"""SQLAlchemy 2.0 declarative models for the Carve state store.

Six tables: `runs`, `logs`, `plans`, `pipelines`, `builds`, and
`workspaces`. The schema is managed by Alembic — see ``migrations/`` —
but the ORM models are still the canonical Python representation that
repository methods return.

v0.1-01 ported the state store from SQLite to Postgres. Concretely:

* `Plan.task_graph_json` and `Build.manifest_json` are now JSONB columns.
  The ORM-side accessor returns a ``dict[str, Any]`` (psycopg's default
  for JSONB), so callers no longer ``json.loads`` the value.
* All timestamp columns are ``TIMESTAMP WITH TIME ZONE``. Defaults still
  produce UTC-aware datetimes; the column round-trips them as UTC.

The repository continues to be the single SQL-issuing module — these
classes are the typed shape of the rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Native Postgres types used by the models. ``TIMESTAMPTZ`` is the column
# type for every timestamp; ``JSONB`` for the three free-form JSON
# payloads. We keep the import tight so the rest of the file stays
# backend-agnostic looking.
_TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)


def _utcnow() -> datetime:
    """Aware UTC ``now()``, used as a default factory.

    v0.1-01 retired the naive-UTC convention from M1 — Postgres handles
    ``TIMESTAMPTZ`` natively. Callers reading rows back get aware
    datetimes; tests that previously stripped ``tzinfo`` should attach
    UTC instead.
    """
    return datetime.now(UTC)


def _default_plan_expiry() -> datetime:
    """Plans expire 24 hours after creation by default."""
    return _utcnow() + timedelta(hours=24)


class Base(DeclarativeBase):
    """Declarative base for all state-store models."""


class Run(Base):
    """A single execution of a plan or pipeline."""

    __tablename__ = "runs"

    __table_args__ = (Index("ix_runs_parent_run_id", "parent_run_id"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str]
    target_id: Mapped[str]
    target: Mapped[str | None] = mapped_column(default=None)
    pipeline_name: Mapped[str | None] = mapped_column(
        ForeignKey("pipelines.name"),
        default=None,
    )
    # Set on recovery-attempt runs; NULL for the original failed run and
    # for everything created before P1-09. The chain is reachable via
    # ``SELECT * FROM runs WHERE parent_run_id = <run_id>``.
    parent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", name="fk_runs_parent_run_id"),
        default=None,
    )
    owner_user_id: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="queued")
    started_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    duration_ms: Mapped[int | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    tokens_input: Mapped[int] = mapped_column(default=0)
    tokens_output: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)


class Log(Base):
    """A single streamed log line attached to a run."""

    __tablename__ = "logs"
    __table_args__ = (Index("ix_logs_run_id_timestamp", "run_id", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    timestamp: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    level: Mapped[str]
    source: Mapped[str]
    message: Mapped[str]


class Plan(Base):
    """Index row for a plan stored on disk at ``.carve/plans/<id>.json``.

    Plan is now strictly a *design* artifact: it captures what the user
    asked for and what the planner agreed to build, but it carries no
    deploy/build state. P1-02 dropped ``estimates_json``, ``deployed_at``,
    and ``deploy_run_id`` — the corresponding state moved to the new
    `Build` table (which also owns the per-target binding).

    Two columns added in M1.1-06 remain:

    * ``phase`` — ``drafted`` (default) or ``built``. CHECK constraint
      enforces those two values; transitions are driven by the build
      flow, never written by hand.
    * ``pipeline_name`` — set during ``carve build`` to the name the
      build agent landed on. ``NULL`` while the plan is still in the
      drafted phase.

    v0.1-01 changed ``task_graph_json`` from TEXT to JSONB. The ORM
    surface returns ``dict[str, Any]`` directly — callers no longer
    parse the string themselves.
    """

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('drafted', 'built')",
            name="ck_plans_phase",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    parent_plan_id: Mapped[str | None] = mapped_column(default=None)
    goal: Mapped[str]
    config_hash: Mapped[str]
    carve_version: Mapped[str]
    task_graph_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    file_path: Mapped[str]
    phase: Mapped[str] = mapped_column(default="drafted")
    pipeline_name: Mapped[str | None] = mapped_column(
        ForeignKey("pipelines.name"),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_default_plan_expiry)


class Pipeline(Base):
    """A first-class pipeline asset.

    The directory under ``el/<name>/`` is the source of truth for code
    (P1.1-01 flattened the layout). This row exists so the CLI/UI can
    list pipelines, filter their runs, and walk plan/build lineage
    without re-reading every file.

    The ``last_run_*`` columns are denormalised from `runs` so that
    ``carve pipelines`` doesn't need a per-row JOIN. They are updated by
    `Repository.record_pipeline_run` when a run reaches a terminal state.

    P1-02 replaced ``current_plan_id`` with ``current_build_id`` — the
    deployable artifact is now Build, and Plan is reachable via
    ``Build.plan_id``.
    """

    __tablename__ = "pipelines"

    name: Mapped[str] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(default="")
    pipeline_dir: Mapped[str]
    current_build_id: Mapped[str | None] = mapped_column(
        ForeignKey("builds.id"),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    last_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id"),
        default=None,
    )
    last_run_status: Mapped[str | None] = mapped_column(default=None)
    last_run_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)


class Build(Base):
    """A deployable artifact produced by ``carve build``.

    Every successful build creates one row. The build binds a Plan
    (the *design*) to a target (``dev``, ``prod``, etc.) and a manifest
    of files written. ``carve el deploy`` consumes the manifest to ship
    the build to its target.

    Indexed on ``(pipeline_name, target, created_at DESC)`` so
    "latest build of <name> for <target>" stays a cheap lookup.

    v0.1-01 changed ``manifest_json`` from TEXT to JSONB; callers read
    a ``dict[str, Any]`` directly.
    """

    __tablename__ = "builds"
    __table_args__ = (
        Index(
            "ix_builds_pipeline_target_created_at",
            "pipeline_name",
            "target",
            sa.text("created_at DESC"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    pipeline_name: Mapped[str] = mapped_column(ForeignKey("pipelines.name"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    target: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, default=_utcnow)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=lambda: {"files": []},
    )
    commit_sha: Mapped[str | None] = mapped_column(default=None)
    pr_url: Mapped[str | None] = mapped_column(default=None)
    deployed_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)


class Workspace(Base):
    """Diagnostics row for a cached ``separate-remote`` component.

    The control plane clones each ``separate-remote`` component into
    ``<root>/.carve/workspaces/<name>/`` (see
    ``carve.integrations.workspace_cache``). This table records the sync
    result so the static UI can show, per cached repo, what revision it's
    on and whether it's healthy. It is **diagnostics only** — the source
    of truth for the code is the on-disk clone, not this row. The heavy
    querying is the UI's (Increment 5); the repository keeps a thin
    upsert/read surface.

    ``name`` is the workspace's derived cache-dir name (``slug(url)`` +
    branch/ref), which is unique per (url, revision) and stable across
    syncs — hence the primary key. ``status`` is constrained to the three
    documented values.
    """

    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('clean', 'dirty', 'unreachable')",
            name="ck_workspaces_status",
        ),
    )

    name: Mapped[str] = mapped_column(primary_key=True)
    url: Mapped[str]
    branch: Mapped[str | None] = mapped_column(default=None)
    last_synced_commit: Mapped[str | None] = mapped_column(default=None)
    last_synced_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    status: Mapped[str] = mapped_column(default="clean")


__all__: list[Any] = [
    "Base",
    "Build",
    "Log",
    "Pipeline",
    "Plan",
    "Run",
    "Workspace",
]
