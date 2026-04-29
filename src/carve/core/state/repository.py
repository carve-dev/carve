"""Typed repository — the only module that issues SQL.

CLI commands, agents, and runners construct a `Repository` and call its
methods. They never open a `Session` directly. Each method opens a short
transaction, commits, and returns plain ORM objects (or simple Python
values). `expire_on_commit=False` on the session factory means the
returned instances are safely detached and can be read after commit.

For M1 the surface is small but covers everything the agent loop and CLI
need: create/update/list runs, append/read logs, and save/get/list/expire
plans. M2 will add step-level state and richer filters.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from carve.core.state.models import Log, Plan, Run

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


class Repository:
    """Typed access to the state store.

    Construct once per process (or per request, for the future API
    server) and pass the instance to anything that needs to read or
    write state.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ Runs

    def create_run(self, kind: str, target_id: str) -> str:
        """Insert a new `runs` row in the default `queued` state.

        Returns the generated run id (a UUID4 hex string). The id is
        chosen client-side so the caller can stream logs against it
        before the row is committed.
        """
        run_id = uuid.uuid4().hex
        with self._session_factory() as session:
            session.add(Run(id=run_id, kind=kind, target_id=target_id))
            session.commit()
        return run_id

    def update_run_status(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Move a run to a new lifecycle state.

        Side-effects mirror the spec's lifecycle:
        - Setting `status="running"` populates `started_at` if unset.
        - Any terminal status (`success`, `failed`, `cancelled`, `crashed`)
          populates `completed_at` and computes `duration_ms` from
          `started_at`.
        - `error` is stored verbatim on `error_message`.
        """
        terminal = {"success", "failed", "cancelled", "crashed"}
        with self._session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(f"run {run_id!r} not found")
            run.status = status
            if error is not None:
                run.error_message = error
            now_utc = datetime.now(UTC).replace(tzinfo=None)
            if status == "running" and run.started_at is None:
                run.started_at = now_utc
            if status in terminal:
                run.completed_at = now_utc
                if run.started_at is not None:
                    delta = now_utc - run.started_at
                    run.duration_ms = int(delta.total_seconds() * 1000)
            session.commit()

    def get_run(self, run_id: str) -> Run | None:
        """Fetch a run by id, or `None` if not found."""
        with self._session_factory() as session:
            return session.get(Run, run_id)

    def list_runs(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Run]:
        """List runs newest-first, optionally filtered by status."""
        stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(Run.status == status)
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    # ------------------------------------------------------------------ Logs

    def append_log(
        self,
        run_id: str,
        level: str,
        source: str,
        message: str,
    ) -> None:
        """Append a log line for `run_id`.

        The autoincrement primary key plus the default timestamp give us
        a deterministic ordering for log tails even when multiple lines
        arrive in the same millisecond.
        """
        with self._session_factory() as session:
            session.add(
                Log(
                    run_id=run_id,
                    level=level,
                    source=source,
                    message=message,
                )
            )
            session.commit()

    def get_logs(
        self,
        run_id: str,
        since: datetime | None = None,
    ) -> list[Log]:
        """Return logs for a run in insertion order.

        `since` is exclusive: callers tailing logs pass the timestamp
        of the last line they've seen and get strictly newer ones back.
        """
        stmt = select(Log).where(Log.run_id == run_id).order_by(Log.id.asc())
        if since is not None:
            stmt = stmt.where(Log.timestamp > since)
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    # ----------------------------------------------------------------- Plans

    def save_plan(self, plan: Plan) -> None:
        """Insert or update a plan row.

        The ORM object is detached after commit; callers may keep using
        it for read-only access.
        """
        with self._session_factory() as session:
            session.merge(plan)
            session.commit()

    def get_plan(self, plan_id: str) -> Plan | None:
        """Fetch a plan by id, or `None` if not found."""
        with self._session_factory() as session:
            return session.get(Plan, plan_id)

    def list_plans(self, limit: int = 50) -> list[Plan]:
        """List plans newest-first."""
        stmt = select(Plan).order_by(Plan.created_at.desc()).limit(limit)
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def list_expired_plans(self, now: datetime | None = None) -> list[Plan]:
        """Return un-applied plans whose `expires_at` is in the past.

        `now` is injectable for tests; production callers pass `None` and
        get the current UTC time.
        """
        cutoff = now if now is not None else datetime.now(UTC).replace(tzinfo=None)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.astimezone(UTC).replace(tzinfo=None)
        stmt = (
            select(Plan)
            .where(Plan.expires_at < cutoff)
            .where(Plan.applied_at.is_(None))
            .order_by(Plan.created_at.asc())
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def expire_old_plans(self, now: datetime | None = None) -> int:
        """Convenience: count of currently-expired, un-applied plans.

        For M1 we don't delete or rewrite anything — the index stays
        and the on-disk JSON is the source of truth. Callers use the
        count to surface a hint in the CLI; M2 may switch to a
        soft-delete column.
        """
        return len(self.list_expired_plans(now=now))
