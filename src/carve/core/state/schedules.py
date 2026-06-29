"""The schedules repository — the scheduler's source of truth + its audit trail.

``schedules`` is DATA: the scheduler reads it (``list_due``), fires due rows, and
advances ``next_fires_at`` (``set_last_fired``); ``carve schedule`` mutates it
live (``pause``/``resume``/``set_cron``). Every live mutation runs in **one
transaction** that (a) updates the ``schedules`` row, (b) recomputes
``next_fires_at`` via the cron module on a cron/timezone change, (c) appends a
``schedule_changes`` audit row (``before``/``after`` JSONB snapshots), and (d)
calls the :meth:`_emit` event seam — so the row, its audit trail, and the
recomputed firing time can never drift apart.

Mirrors :class:`~carve.core.state.job_queue.JobQueue`: constructed from the same
``sessionmaker`` (``expire_on_commit=False`` keeps returned rows readable after
commit), short sync transactions, raw SQL only where the ORM can't express it.

The load-bearing invariant (Working notes §1): a fire MUST advance
``next_fires_at`` to the FOLLOWING tick in the same transaction. Otherwise the
row stays due (``next_fires_at <= now``), rides the partial ``ix_schedules_due``,
and re-fires every loop tick — deduped only by ``QueuedJobAlreadyExists``, which
is the wrong reason. ``set_last_fired`` and ``set_cron`` both recompute it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from carve.core.state.models import Schedule, ScheduleChange

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    # Typed under TYPE_CHECKING only — same circular-import dodge as
    # ``_next_tick_after``'s lazy ``carve.runtime.cron`` import below: a runtime
    # ``from carve.runtime.events import EventSink`` would re-enter the
    # ``carve.runtime`` package mid-import. ``from __future__ import annotations``
    # stringizes the annotation, so the state store carries no runtime import.
    from carve.runtime.events import EventSink


def _next_tick_after(cron: str, after: datetime, timezone: str) -> datetime:
    """Lazy bridge to ``carve.runtime.cron.next_tick_after``.

    Imported at call time, not module top, to avoid a circular import: the
    ``carve.runtime`` package's ``__init__`` eagerly pulls in the worker/step
    chain, which imports back into ``carve.core.state`` — a top-level import here
    would deadlock that cycle. The cron module itself only depends on croniter +
    zoneinfo, so the deferred import is cheap.
    """
    from carve.runtime.cron import next_tick_after

    return next_tick_after(cron, after, timezone)


class ScheduleNotFound(KeyError):
    """Raised when a mutator/reader targets a pipeline with no ``schedules`` row.

    ``pause``/``resume`` (and ``show``) require an existing row; ``set_cron``
    and ``seed`` create one if absent (so the CLI can stand a schedule up
    end-to-end without the deferred reconciler-seed).
    """


# The fields snapshotted into ``schedule_changes.before``/``after``. JSON-safe
# (datetimes are ISO strings) so they round-trip cleanly through JSONB.
_SNAPSHOT_FIELDS = (
    "cron",
    "target",
    "timezone",
    "paused",
    "paused_by",
    "pause_reason",
    "next_fires_at",
)


def _snapshot(schedule: Schedule) -> dict[str, Any]:
    """A JSON-safe snapshot of a schedule's mutable fields (for the audit row)."""
    snap: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        value = getattr(schedule, field)
        snap[field] = value.isoformat() if isinstance(value, datetime) else value
    return snap


def _cron_tz(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project a full ``_snapshot`` down to the ``schedule.changed`` event's
    ``before``/``after`` (cron + timezone only), or ``None`` for a create path."""
    if snapshot is None:
        return None
    return {"cron": snapshot["cron"], "timezone": snapshot["timezone"]}


class Schedules:
    """Typed access to the ``schedules``/``schedule_changes`` tables.

    Construct once per process from the same ``sessionmaker`` as
    :class:`~carve.core.state.repository.Repository`. Every method opens a short
    sync transaction and returns detached ORM objects (or plain values).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        emitter: EventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        # The injected event sink (the concrete ``EventEmitter`` in production).
        # ``None`` ⇒ ``_emit`` stays a silent no-op, so every existing
        # caller/test that constructs ``Schedules(factory)`` is unchanged.
        self._emitter = emitter

    # ------------------------------------------------------------------- Reads

    def get(self, pipeline: str, *, tenant_id: int = 1) -> Schedule | None:
        """Fetch a schedule by pipeline, or ``None`` if absent."""
        stmt = sa.select(Schedule).where(
            Schedule.pipeline == pipeline,
            Schedule.tenant_id == tenant_id,
        )
        with self._session_factory() as session:
            return session.scalars(stmt).one_or_none()

    def list_all(self, *, tenant_id: int = 1) -> list[Schedule]:
        """Return all schedules for a tenant, ordered by pipeline (for ``list``)."""
        stmt = (
            sa.select(Schedule)
            .where(Schedule.tenant_id == tenant_id)
            .order_by(Schedule.pipeline.asc())
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def list_due(self, now: datetime, *, tenant_id: int = 1) -> list[Schedule]:
        """Return unpaused schedules whose ``next_fires_at`` has passed.

        ``WHERE paused = false AND next_fires_at <= now`` — rides the partial
        ``ix_schedules_due`` index. A row with ``next_fires_at IS NULL`` (never
        seeded a fire time) is not due. The scheduler fires these, then
        ``set_last_fired`` advances each ``next_fires_at`` so the same window
        doesn't re-fire next tick.
        """
        stmt = (
            sa.select(Schedule)
            .where(
                Schedule.tenant_id == tenant_id,
                Schedule.paused.is_(False),
                Schedule.next_fires_at.is_not(None),
                Schedule.next_fires_at <= now,
            )
            .order_by(Schedule.next_fires_at.asc())
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    # ------------------------------------------------------------- Row creation

    def seed(
        self,
        pipeline: str,
        cron: str,
        target: str,
        *,
        timezone: str = "UTC",
        tenant_id: int = 1,
    ) -> Schedule:
        """Create-or-update a schedule from a ``(cron, timezone, target)`` triple.

        This is the forward-compatible seam the future reconciler will call (it
        is exactly the ``SeedSchedule`` shape, which forbids ``paused`` — pausing
        is live data, never seeded). Computes the initial ``next_fires_at`` =
        the next tick strictly after ``now``. Idempotent on ``(pipeline,
        tenant_id)``: a second seed updates ``cron``/``timezone``/``target`` and
        recomputes ``next_fires_at`` without disturbing a live pause. Records a
        ``schedule_changes`` row with ``source='seed'``.
        """
        now = _utcnow()
        next_fires = _next_tick_after(cron, now, timezone)
        with self._session_factory() as session:
            schedule = self._get_locked(session, pipeline, tenant_id)
            if schedule is None:
                before: dict[str, Any] | None = None
                schedule = Schedule(
                    id="sched_" + uuid.uuid4().hex,
                    pipeline=pipeline,
                    cron=cron,
                    target=target,
                    timezone=timezone,
                    tenant_id=tenant_id,
                    next_fires_at=next_fires,
                    created_at=now,
                    updated_at=now,
                )
                session.add(schedule)
            else:
                before = _snapshot(schedule)
                schedule.cron = cron
                schedule.target = target
                schedule.timezone = timezone
                schedule.next_fires_at = next_fires
                schedule.updated_at = now
            session.flush()
            after = _snapshot(schedule)
            self._append_change(
                session,
                pipeline=pipeline,
                change_kind="reseed" if before is not None else "set_cron",
                before=before,
                after=after,
                source="seed",
                reason=None,
                actor_token_id=None,
                tenant_id=tenant_id,
                changed_at=now,
            )
            session.commit()
            self._emit(
                "schedule.seeded",
                {"pipeline": pipeline, "cron": cron, "source": "seed"},
            )
            session.refresh(schedule)
            return schedule

    # --------------------------------------------------------------- Scheduler

    def set_last_fired(self, schedule_id: str, now: datetime) -> Schedule:
        """Stamp ``last_fired_at`` AND advance ``next_fires_at`` — one transaction.

        The load-bearing recompute: ``next_fires_at`` moves to the FOLLOWING cron
        tick (strictly after the just-fired window's ``next_fires_at``, or after
        ``now`` if it was NULL), so the row leaves the due window and does not
        re-fire on the next loop tick. No audit row — a fire is not a live
        *change* (the ``schedule_changes`` trail is for user/recovery mutations).
        """
        with self._session_factory() as session:
            schedule = session.get(Schedule, schedule_id)
            if schedule is None:
                raise ScheduleNotFound(f"schedule {schedule_id!r} not found")
            anchor = schedule.next_fires_at if schedule.next_fires_at is not None else now
            schedule.last_fired_at = now
            schedule.next_fires_at = _next_tick_after(schedule.cron, anchor, schedule.timezone)
            schedule.updated_at = now
            session.commit()
            session.refresh(schedule)
            return schedule

    # ----------------------------------------------------------- Live mutators

    def pause(
        self,
        pipeline: str,
        *,
        reason: str | None = None,
        source: str = "cli",
        actor_token_id: str | None = None,
        tenant_id: int = 1,
    ) -> Schedule:
        """Pause a schedule (origin ``user``) + audit — one transaction.

        Sets ``paused=true, paused_by='user', pause_reason=reason`` (honoring
        ``ck_schedules_pause_origin``), appends a ``schedule_changes`` row
        (``change_kind='pause'``), and calls the ``schedule.paused`` emit seam.
        ``next_fires_at`` is left intact: ``list_due``'s partial index already
        excludes a paused row, and keeping the firing time means resume picks up
        cleanly without a recompute.
        """
        return self._mutate(
            pipeline,
            change_kind="pause",
            emit_kind="schedule.paused",
            tenant_id=tenant_id,
            source=source,
            reason=reason,
            actor_token_id=actor_token_id,
            apply=lambda schedule: _apply_pause(schedule, reason),
        )

    def resume(
        self,
        pipeline: str,
        *,
        reason: str | None = None,
        source: str = "cli",
        actor_token_id: str | None = None,
        tenant_id: int = 1,
    ) -> Schedule:
        """Resume a schedule + audit — one transaction.

        Clears ``paused=false, paused_by=NULL, pause_reason=NULL`` (honoring the
        CHECK), appends ``change_kind='resume'``, emits ``schedule.resumed``.
        """
        return self._mutate(
            pipeline,
            change_kind="resume",
            emit_kind="schedule.resumed",
            tenant_id=tenant_id,
            source=source,
            reason=reason,
            actor_token_id=actor_token_id,
            apply=_apply_resume,
        )

    def set_cron(
        self,
        pipeline: str,
        cron: str,
        *,
        target: str | None = None,
        timezone: str | None = None,
        reason: str | None = None,
        source: str = "cli",
        actor_token_id: str | None = None,
        tenant_id: int = 1,
    ) -> Schedule:
        """Change a schedule's ``cron`` (+ optional ``timezone``/``target``) + audit.

        One transaction: update ``cron`` (and ``timezone``/``target`` if given),
        **recompute ``next_fires_at``** via the cron module, append
        ``change_kind='set_cron'``, emit ``schedule.changed``. **UPSERTs** — if no
        row exists for ``pipeline`` it is created (so the CLI can stand up a
        schedule end-to-end without the deferred reconciler-seed); a created row
        needs a ``target``, defaulting to ``'prod'`` when none is supplied.
        """
        now = _utcnow()
        with self._session_factory() as session:
            schedule = self._get_locked(session, pipeline, tenant_id)
            if schedule is None:
                before: dict[str, Any] | None = None
                schedule = Schedule(
                    id="sched_" + uuid.uuid4().hex,
                    pipeline=pipeline,
                    cron=cron,
                    target=target if target is not None else "prod",
                    timezone=timezone if timezone is not None else "UTC",
                    tenant_id=tenant_id,
                    created_at=now,
                    updated_at=now,
                )
                schedule.next_fires_at = _next_tick_after(schedule.cron, now, schedule.timezone)
                session.add(schedule)
            else:
                before = _snapshot(schedule)
                schedule.cron = cron
                if target is not None:
                    schedule.target = target
                if timezone is not None:
                    schedule.timezone = timezone
                schedule.next_fires_at = _next_tick_after(schedule.cron, now, schedule.timezone)
                schedule.updated_at = now
            session.flush()
            after = _snapshot(schedule)
            self._append_change(
                session,
                pipeline=pipeline,
                change_kind="set_cron",
                before=before,
                after=after,
                source=source,
                reason=reason,
                actor_token_id=actor_token_id,
                tenant_id=tenant_id,
                changed_at=now,
            )
            session.commit()
            # Taxonomy (schedule.changed): pipeline, before (cron/tz), after
            # (cron/tz), actor_token_id, source, reason. ``before`` is None when
            # set_cron stood the row up (the UPSERT create path).
            self._emit(
                "schedule.changed",
                {
                    "pipeline": pipeline,
                    "before": _cron_tz(before),
                    "after": _cron_tz(after),
                    "actor_token_id": actor_token_id,
                    "source": source,
                    "reason": reason,
                },
            )
            session.refresh(schedule)
            return schedule

    def list_changes(self, pipeline: str, *, tenant_id: int = 1) -> list[ScheduleChange]:
        """Return a pipeline's audit rows, newest first (for tests/``show``)."""
        stmt = (
            sa.select(ScheduleChange)
            .where(
                ScheduleChange.pipeline == pipeline,
                ScheduleChange.tenant_id == tenant_id,
            )
            .order_by(ScheduleChange.changed_at.desc(), ScheduleChange.id.desc())
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    # --------------------------------------------------------------- Internals

    def _mutate(
        self,
        pipeline: str,
        *,
        change_kind: str,
        emit_kind: str,
        tenant_id: int,
        source: str,
        reason: str | None,
        actor_token_id: str | None,
        apply: Any,
    ) -> Schedule:
        """Shared one-transaction body for ``pause``/``resume`` (require an existing row)."""
        now = _utcnow()
        with self._session_factory() as session:
            schedule = self._get_locked(session, pipeline, tenant_id)
            if schedule is None:
                raise ScheduleNotFound(f"no schedule for pipeline {pipeline!r}")
            before = _snapshot(schedule)
            apply(schedule)
            schedule.updated_at = now
            session.flush()
            after = _snapshot(schedule)
            self._append_change(
                session,
                pipeline=pipeline,
                change_kind=change_kind,
                before=before,
                after=after,
                source=source,
                reason=reason,
                actor_token_id=actor_token_id,
                tenant_id=tenant_id,
                changed_at=now,
            )
            session.commit()
            # Taxonomy (schedule.paused / schedule.resumed): pipeline,
            # actor_token_id, source, reason.
            self._emit(
                emit_kind,
                {
                    "pipeline": pipeline,
                    "actor_token_id": actor_token_id,
                    "source": source,
                    "reason": reason,
                },
            )
            session.refresh(schedule)
            return schedule

    def _get_locked(self, session: Session, pipeline: str, tenant_id: int) -> Schedule | None:
        """Row-lock the schedule for the mutating transaction (``FOR UPDATE``)."""
        stmt = (
            sa.select(Schedule)
            .where(Schedule.pipeline == pipeline, Schedule.tenant_id == tenant_id)
            .with_for_update()
        )
        return session.scalars(stmt).one_or_none()

    def _append_change(
        self,
        session: Session,
        *,
        pipeline: str,
        change_kind: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        source: str,
        reason: str | None,
        actor_token_id: str | None,
        tenant_id: int,
        changed_at: datetime,
    ) -> None:
        """Insert the ``schedule_changes`` audit row inside the current txn."""
        session.add(
            ScheduleChange(
                pipeline=pipeline,
                change_kind=change_kind,
                before=before,
                after=after,
                actor_token_id=actor_token_id,
                source=source,
                reason=reason,
                tenant_id=tenant_id,
                changed_at=changed_at,
            )
        )

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        """The event-emit seam — delegates to the injected :class:`EventSink`.

        The scheduler's ``schedule.fired``/``schedule.skipped`` and the mutators'
        ``schedule.paused``/``resumed``/``changed``/``seeded`` all flow through
        this single method. With **no** emitter injected it is a silent no-op
        (the back-compat path the existing ``_emit``-spy tests rely on); with one
        injected it writes a durable ``events`` row (best-effort — the emitter
        swallows its own failures).
        """
        if self._emitter is not None:
            self._emitter.emit(kind, payload)


def _apply_pause(schedule: Schedule, reason: str | None) -> None:
    schedule.paused = True
    schedule.paused_by = "user"
    schedule.pause_reason = reason


def _apply_resume(schedule: Schedule) -> None:
    schedule.paused = False
    schedule.paused_by = None
    schedule.pause_reason = None


def _utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


__all__ = [
    "ScheduleNotFound",
    "Schedules",
]
