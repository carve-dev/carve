"""The :class:`StateStore` facade — one bundle of every state-store repo.

The rest-api spec's ``create_app(state_store, config)`` and its middleware use
attribute access (``state_store.tokens``, ``state_store.webhook_deliveries``,
``state_store.repository``); the runtime historically constructs
``Repository``/``JobQueue``/``Schedules``/``EventEmitter`` *individually* from one
shared ``session_factory`` (``cli/commands/serve.py``). This facade bundles the
existing repos plus the new REST repos (``Tokens``/``Webhooks``/
``WebhookDeliveries``/``IdempotencyKeys``/``EventsReader``) from a single
``session_factory`` and exposes them as attributes — satisfying the API's shape
**without** re-plumbing existing call sites, which keep using their own repos.

Construct once per process. ``carve serve`` builds it from the same
``session_factory`` (+ shared ``EventEmitter``) it already wires; ``create_app``
just receives it. The ``emitter`` is passed in (not imported) so this module
keeps the state store free of any ``carve.runtime.*`` top-level import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from carve.core.state.events_read import EventsReader
from carve.core.state.idempotency import IdempotencyKeys
from carve.core.state.job_queue import JobQueue
from carve.core.state.repository import Repository
from carve.core.state.schedules import Schedules
from carve.core.state.tokens import Tokens
from carve.core.state.webhooks import WebhookDeliveries, Webhooks

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from carve.core.observability.rollups import MetricsRollups
    from carve.runtime.events import EventSink


class StateStore:
    """A thin attribute bundle of the state-store repositories."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        emitter: EventSink | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.emitter = emitter
        #: Runs / plans / builds / pipelines / workspaces.
        self.repository = Repository(session_factory)
        #: Jobs + workers + step_runs.
        self.jobs = JobQueue(session_factory, emitter=emitter)
        self.schedules = Schedules(session_factory, emitter=emitter)
        self.tokens = Tokens(session_factory)
        self.webhooks = Webhooks(session_factory)
        self.webhook_deliveries = WebhookDeliveries(session_factory)
        self.idempotency_keys = IdempotencyKeys(session_factory)
        self.events = EventsReader(session_factory)

    @property
    def metrics(self) -> MetricsRollups:
        """The observability read-side (``carve metrics``).

        Constructed lazily to avoid a state→observability import at module load.
        """
        from carve.core.observability.rollups import MetricsRollups

        return MetricsRollups(self.session_factory)


__all__ = ["StateStore"]
