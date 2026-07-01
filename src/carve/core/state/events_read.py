"""Read-side over the ``events`` log, scoped to a run — the streaming source.

The runtime writes run-lifecycle events (``run.started``/``run.succeeded``/
``run.failed``, ``step.*``) whose ``payload`` carries the ``run_id`` (see
``runtime/worker.py``). :class:`EventsReader` selects those rows for one run:
:meth:`backfill` replays everything since a cursor, and :meth:`tail_after`
returns rows newer than a given event id — the poll the WebSocket/SSE stream in
:mod:`carve.api.streams` rides. Read-only; no runtime import (the state store
never top-level-imports ``carve.runtime.*``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

import sqlalchemy as sa

from carve.core.state.models import Event

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

#: Event kinds that terminate a run's stream.
TERMINAL_RUN_EVENTS = frozenset(
    {"run.succeeded", "run.failed", "run.completed", "run.cancelled", "run.crashed"}
)


class EventsReader:
    """Run-scoped reads over the ``events`` table."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def _run_filter(self, run_id: str) -> sa.ColumnElement[bool]:
        # ``payload->>'run_id'`` — the JSONB text accessor. Run/step events carry
        # ``run_id`` in their payload (runtime/worker.py + persisting_step_sink).
        return cast("sa.ColumnElement[bool]", Event.payload["run_id"].astext == run_id)

    def backfill(
        self, run_id: str, *, since: datetime | None = None, limit: int = 1000
    ) -> list[Event]:
        """Return this run's events (oldest first), optionally since ``since``."""
        stmt = sa.select(Event).where(self._run_filter(run_id))
        if since is not None:
            stmt = stmt.where(Event.occurred_at >= since)
        stmt = stmt.order_by(Event.id.asc()).limit(limit)
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def get(self, event_id: int) -> Event | None:
        """Fetch a single event by id (the webhook publisher's payload source)."""
        with self._session_factory() as session:
            return session.get(Event, event_id)

    def tail_after(self, run_id: str, *, after_id: int, limit: int = 500) -> list[Event]:
        """Return this run's events with ``id > after_id`` (oldest first)."""
        stmt = (
            sa.select(Event)
            .where(self._run_filter(run_id), Event.id > after_id)
            .order_by(Event.id.asc())
            .limit(limit)
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())


__all__ = ["TERMINAL_RUN_EVENTS", "EventsReader"]
