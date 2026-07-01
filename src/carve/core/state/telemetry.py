"""The telemetry repository — the single writer for the agent-telemetry tables.

``TelemetryRepo`` is to ``agents``/``agent_invocations``/``skill_calls`` what
:class:`~carve.core.state.schedules.Schedules` is to the scheduler tables: it is
constructed from the same ``sessionmaker`` and is the one module that issues
writes against these tables. The read-side aggregation (``carve metrics``) lives
in :class:`~carve.core.observability.rollups.MetricsRollups`.

The write surface mirrors the recording lifecycle:

* :meth:`upsert_agent` — project a discovered agent into the ``agents`` registry
  row (idempotent on ``name``).
* :meth:`open_invocation` — mint the app-generated ``agent_invocations`` id and
  insert a ``status='running'`` row (ensuring its ``agents`` FK parent exists in
  the same transaction), returning the id so ``skill_calls`` can reference it.
* :meth:`record_skill_call` — append one ``skill_calls`` row against an open
  invocation.
* :meth:`finalize_invocation` — stamp the invocation's terminal tokens / cost /
  duration / status.

Every method opens a short sync transaction and returns detached values, exactly
like the other state-store repos. Recording is best-effort telemetry: the
:class:`~carve.core.observability.recording.RecordingObserver` that drives this
repo contains its own failures (logs, never raises), so a telemetry write failure
never blocks the delegated run — but this module does not itself swallow errors
(a logic bug should surface to the observer's log, not vanish here).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.dialects.postgresql import insert as pg_insert

from carve.core.state.models import Agent, AgentInvocation, SkillCall

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TelemetryRepo:
    """Typed write access to ``agents``/``agent_invocations``/``skill_calls``.

    Construct once per process from the same ``sessionmaker`` as
    :class:`~carve.core.state.repository.Repository`.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # --------------------------------------------------------------- Agents

    def upsert_agent(
        self,
        name: str,
        *,
        model: str | None = None,
        source: str = "builtin",
    ) -> None:
        """Idempotently project an agent into the ``agents`` registry row.

        ``INSERT ... ON CONFLICT (name) DO UPDATE`` on ``model``/``source``/
        ``updated_at`` — a second call refreshes the hint without disturbing the
        JSONB projection the discovery side fills.
        """
        now = _utcnow()
        stmt = pg_insert(Agent).values(
            name=name,
            model=model,
            source=source,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["name"],
            set_={
                "model": stmt.excluded.model,
                "source": stmt.excluded.source,
                "updated_at": now,
            },
        )
        with self._session_factory() as session:
            session.execute(stmt)
            session.commit()

    # ---------------------------------------------------------- Invocations

    def open_invocation(
        self,
        *,
        agent_name: str,
        run_id: str | None = None,
        plan_id: str | None = None,
        build_id: str | None = None,
        ask_id: str | None = None,
        model: str | None = None,
        started_at: datetime | None = None,
    ) -> str:
        """Insert a ``status='running'`` invocation row; return its minted id.

        The ``agents`` FK parent is ensured (``ON CONFLICT DO NOTHING``) in the
        **same transaction** so the invocation insert never trips the FK. The id
        is app-generated (``inv_<uuid>``) so the caller can reference it from
        ``skill_calls`` without a mid-recording flush.
        """
        inv_id = "inv_" + uuid.uuid4().hex
        started = started_at if started_at is not None else _utcnow()
        now = _utcnow()
        ensure_agent = (
            pg_insert(Agent)
            .values(
                name=agent_name,
                model=model,
                source="builtin",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["name"])
        )
        with self._session_factory() as session:
            session.execute(ensure_agent)
            session.add(
                AgentInvocation(
                    id=inv_id,
                    agent_name=agent_name,
                    run_id=run_id,
                    plan_id=plan_id,
                    build_id=build_id,
                    ask_id=ask_id,
                    status="running",
                    started_at=started,
                )
            )
            session.commit()
        return inv_id

    def finalize_invocation(
        self,
        invocation_id: str,
        *,
        tokens_input: int,
        tokens_output: int,
        cost_usd: float,
        duration_ms: int,
        status: str,
        finished_at: datetime | None = None,
    ) -> None:
        """Stamp the invocation's terminal tokens / cost / duration / status."""
        finished = finished_at if finished_at is not None else _utcnow()
        with self._session_factory() as session:
            invocation = session.get(AgentInvocation, invocation_id)
            if invocation is None:
                raise KeyError(f"agent_invocation {invocation_id!r} not found")
            invocation.tokens_input = tokens_input
            invocation.tokens_output = tokens_output
            invocation.cost_usd = cost_usd
            invocation.duration_ms = duration_ms
            invocation.status = status
            invocation.finished_at = finished
            session.commit()

    # ---------------------------------------------------------- Skill calls

    def record_skill_call(
        self,
        *,
        agent_invocation_id: str,
        skill_name: str,
        output_size: int | None = None,
        result_too_large: bool = False,
        pages_walked: int | None = None,
        input_hash: str | None = None,
        duration_ms: int | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """Append one ``skill_calls`` row against an open invocation.

        When only ``duration_ms`` is known (the ``on_tool_result`` case),
        ``started_at`` is derived as ``finished_at - duration_ms`` so the row
        carries a sensible interval.
        """
        finished = finished_at if finished_at is not None else _utcnow()
        started = started_at
        if started is None and duration_ms is not None:
            started = finished - timedelta(milliseconds=duration_ms)
        with self._session_factory() as session:
            session.add(
                SkillCall(
                    agent_invocation_id=agent_invocation_id,
                    skill_name=skill_name,
                    input_hash=input_hash,
                    output_size=output_size,
                    result_too_large=result_too_large,
                    pages_walked=pages_walked,
                    duration_ms=duration_ms,
                    started_at=started,
                    finished_at=finished,
                )
            )
            session.commit()


__all__ = ["TelemetryRepo"]
