"""The durable event emitter — turn the runtime's ``_emit`` seams into rows.

Since the queue/scheduler/worker/reaper slices shipped, every in-scope state
transition has called a no-op ``_emit(kind, payload)`` seam (``job_queue._emit``,
``schedules._emit``, the persisting sink's ``step.*``). This module is the
concrete emitter those seams delegate to once one is injected: :class:`EventEmitter`
writes one durable ``events`` row per :meth:`~EventEmitter.emit`, and
:class:`EventSink` is the structural type the state-store repos accept so they
can be wired **without** importing this module at runtime (see below).

Best-effort, never blocking the work
------------------------------------
Events are observability/audit/webhook substrate — never the run itself. So an
emit failure is **logged and swallowed**, exactly like the heartbeat's stance: a
down DB or a serialization hiccup must not kill a run, a scheduler pass, or a
reaper sweep. The caller's transition has already committed; the event is a
side-record of it.

The circular-import dance
-------------------------
``carve.runtime.__init__`` eagerly imports the worker chain → ``job_queue`` /
``schedules``. If those state-store modules imported :class:`EventEmitter` at
module top, that would re-enter the ``carve.runtime`` package mid-import — the
exact cycle ``schedules.py`` already dodges for ``carve.runtime.cron``. So they
type the injected emitter as the :class:`EventSink` **Protocol referenced under
``TYPE_CHECKING`` only** (``from __future__ import annotations`` stringizes the
annotation), carrying no runtime import of this module. The **CLI** (top-level)
imports the concrete :class:`EventEmitter` and injects it.

Sync, like the rest of the state store
---------------------------------------
:meth:`EventEmitter.emit` is a plain sync transaction (the state store is sync).
Async callers — the worker, the scheduler loop, the persisting sink — bridge it
off the event loop via :func:`asyncio.to_thread`, exactly as they do every other
state-store call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from carve.core.state.models import Event

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class EventSink(Protocol):
    """The structural type the state-store repos accept for their ``_emit`` seam.

    A repo (``JobQueue``/``Schedules``) takes an optional ``emitter: EventSink |
    None`` and, when set, delegates ``_emit(kind, payload)`` to
    :meth:`emit`. Declaring this Protocol **here** (not in the state store) lets
    the repos reference it under ``TYPE_CHECKING`` without a runtime import of
    ``carve.runtime.events`` — the circular-import escape hatch (see module
    docstring). :class:`EventEmitter` is the production implementation.
    """

    def emit(self, kind: str, payload: dict[str, Any], *, tenant_id: int = 1) -> None:
        """Persist one event of ``kind`` carrying ``payload``."""
        ...


class EventEmitter:
    """Write one durable ``events`` row per :meth:`emit`, best-effort.

    Constructed from the **same** ``sessionmaker`` the repositories share, so an
    event is a row in the same Postgres the transition it records lives in.
    ``expire_on_commit=False`` on the factory keeps nothing pinned; each emit is
    a short, self-contained transaction.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def emit(self, kind: str, payload: dict[str, Any], *, tenant_id: int = 1) -> None:
        """Insert one ``events`` row (``kind``/``payload``/``tenant_id``); swallow on failure.

        ``occurred_at`` defaults to ``now()`` (the model default); ``processed_at``
        stays NULL (the row enters the partial ``ix_events_unprocessed`` index for
        a future relay/webhook). A failure to write — a down DB, a serialization
        error — is **logged and swallowed**: events are observability, not the
        work, and must never propagate into the run/loop that emitted them.
        """
        try:
            with self._session_factory() as session:
                session.add(Event(kind=kind, payload=payload, tenant_id=tenant_id))
                session.commit()
        except Exception:
            # Best-effort by design (mirrors the heartbeat's logged-and-swallowed
            # stance). The caller's transition already committed; a missed event
            # is a missed observability record, not a failed run.
            logger.warning("event emit failed (kind=%s); swallowing", kind, exc_info=True)


__all__ = ["EventEmitter", "EventSink"]
