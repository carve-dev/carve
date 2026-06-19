"""The hook event set + the subscription registry.

Five events, in two groups:

* **Wired now** — ``pre_tool`` / ``post_tool``. Their emitters exist (the
  ``AgentLoop`` fires the tool hooks at its gate→pre_tool→execute→post_tool
  seam, ``loop.py``), so a subscription here is fully live this slice.

* **Deferred emitters (seam only)** — ``pre_deploy`` / ``post_build`` /
  ``on_run_failed``. The **subscription mechanism is built and tested**;
  their EMITTERS land in later increments (deploy = Incr 6, build /
  pipelines = Incr 3, runtime ``run.failed`` = Incr 4). The registry
  accepts handlers for these events **without firing them** — firing
  arrives with each emitter. This is a deliberate seam, not missing
  functionality: the slice bar is "the subscription registers and the
  runner gates/clamps/fail-closes it"; the end-to-end "a ``pre_deploy``
  hook blocks a deploy" is verified with the deploy emitter at its own
  increment.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

# A handler receives the event's payload (named keys) and runs for its
# side effect (the gated shell command / a block). It returns nothing; a
# hook that must block the action raises (the runner is fail-closed).
HookHandler = Callable[[dict[str, Any]], None]


class HookEvent(StrEnum):
    """The known hook events (the ``on =`` values in ``hooks.toml``)."""

    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    PRE_DEPLOY = "pre_deploy"
    POST_BUILD = "post_build"
    ON_RUN_FAILED = "on_run_failed"


# Events whose emitters exist in this slice (the loop fires them). A
# subscription to one of these is live immediately.
EMITTED_EVENTS: frozenset[HookEvent] = frozenset(
    {HookEvent.PRE_TOOL, HookEvent.POST_TOOL}
)

# Events whose subscription is wired but whose emitter is a later
# increment. Registering a handler here is allowed (and tested); it simply
# never fires until the owning increment emits the event.
DEFERRED_EMITTER_EVENTS: frozenset[HookEvent] = frozenset(
    {HookEvent.PRE_DEPLOY, HookEvent.POST_BUILD, HookEvent.ON_RUN_FAILED}
)


class DeferredEmitterEvent(RuntimeError):
    """Raised if code tries to *emit* a not-yet-wired event this slice.

    Subscribing to ``pre_deploy``/``post_build``/``on_run_failed`` is fine
    (the seam). Attempting to fire one *now* is a programming error — the
    emitter belongs to a later increment — so :meth:`HookRegistry.emit`
    refuses it loudly rather than silently dropping handlers.
    """


class HookRegistry:
    """Subscribe :data:`HookHandler` callables per :class:`HookEvent`.

    Handlers are kept in registration order and fired in that order on
    :meth:`emit`. Subscribing to a deferred-emitter event is allowed (the
    seam); *emitting* one this slice raises :class:`DeferredEmitterEvent`.
    """

    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[HookHandler]] = {
            event: [] for event in HookEvent
        }

    def subscribe(self, event: HookEvent, handler: HookHandler) -> None:
        """Register ``handler`` for ``event`` (any event, incl. deferred)."""
        self._handlers[event].append(handler)

    def handlers_for(self, event: HookEvent) -> list[HookHandler]:
        """Return the handlers subscribed to ``event`` (in order)."""
        return list(self._handlers[event])

    def has_handlers(self, event: HookEvent) -> bool:
        return bool(self._handlers[event])

    def emit(self, event: HookEvent, payload: dict[str, Any]) -> None:
        """Fire every handler for ``event`` with ``payload``, in order.

        Refuses to emit a deferred-emitter event this slice (its emitter
        is a later increment). A handler that raises propagates — the
        caller (the loop / the emitter) treats a raising hook as an abort
        (fail-closed). Handlers run sequentially; a raise stops the rest.
        """
        if event in DEFERRED_EMITTER_EVENTS:
            raise DeferredEmitterEvent(
                f"Event {event.value!r} has no emitter in this increment; "
                "subscriptions are accepted as a seam but emission is "
                "deferred to its owning increment."
            )
        for handler in self._handlers[event]:
            handler(payload)


__all__ = [
    "DEFERRED_EMITTER_EVENTS",
    "EMITTED_EVENTS",
    "DeferredEmitterEvent",
    "HookEvent",
    "HookHandler",
    "HookRegistry",
]
