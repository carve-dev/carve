"""SQLAlchemy 2.0 declarative models for the M1 state store.

Three tables only — `runs`, `logs`, `plans`. The schema is intentionally
minimal: M2 and M3 will add columns through alembic migrations once the
shape stabilises. Every column type here is chosen to round-trip cleanly
across SQLite and Postgres.

Defaults that need server-side semantics (timestamps, autoincrement keys)
use SQLAlchemy's column defaults rather than Python-side mutation, so the
same models work with both backends.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    """Naive UTC `now()`, used as a default factory.

    Stored as naive UTC because SQLite drops tzinfo on round-trip; using
    naive everywhere keeps comparisons simple and works identically on
    SQLite and Postgres. Callers that need an aware datetime should
    attach `UTC` themselves at the boundary.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def _default_plan_expiry() -> datetime:
    """Plans expire 24 hours after creation by default."""
    return _utcnow() + timedelta(hours=24)


class Base(DeclarativeBase):
    """Declarative base for all state-store models."""


class Run(Base):
    """A single execution of a plan or pipeline."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str]
    target_id: Mapped[str]
    owner_user_id: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="queued")
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    duration_ms: Mapped[int | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    tokens_input: Mapped[int] = mapped_column(default=0)
    tokens_output: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class Log(Base):
    """A single streamed log line attached to a run."""

    __tablename__ = "logs"
    __table_args__ = (Index("ix_logs_run_id_timestamp", "run_id", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    timestamp: Mapped[datetime] = mapped_column(default=_utcnow)
    level: Mapped[str]
    source: Mapped[str]
    message: Mapped[str]


class Plan(Base):
    """Index row for a plan stored on disk at ``.carve/plans/<id>.json``.

    The canonical plan is the JSON file; the row exists so the CLI/UI can
    list, filter, and join plans against runs without re-reading every
    file. ``estimates_json`` and ``task_graph_json`` are kept here as
    plain TEXT (JSON-encoded) for cheap querying and to avoid a second
    disk read for the listing view.
    """

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(primary_key=True)
    parent_plan_id: Mapped[str | None] = mapped_column(default=None)
    goal: Mapped[str]
    config_hash: Mapped[str]
    carve_version: Mapped[str]
    estimates_json: Mapped[str]
    task_graph_json: Mapped[str]
    file_path: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(default=_default_plan_expiry)
    applied_at: Mapped[datetime | None] = mapped_column(default=None)
    apply_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id"),
        default=None,
    )


# Re-export the helper for callers that want to construct rows in tests with
# a stable "now" they can compare against.
__all__: list[Any] = ["Base", "Log", "Plan", "Run"]
