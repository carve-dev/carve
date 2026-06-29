"""The hook event set + the subscription registry.

Five events, in two groups:

* **Wired now** — ``pre_tool`` / ``post_tool`` / ``post_build``. The tool
  hooks' emitter is the ``AgentLoop`` (it fires them at its
  gate→pre_tool→execute→post_tool seam, ``loop.py``); ``post_build``'s
  emitter is the **build flow** (``cli/orchestrator/builder.py`` fires it
  after a ``Build`` row is durably recorded — plan-build Unit 2). A
  subscription to any of these is fully live.

* **Wired (events slice)** — ``on_run_failed``. The runtime worker fires it
  at its ``run.failed`` transition (``runtime/worker.py``), gated/clamped/
  fail-closed through the same :class:`~carve.core.hooks.runner.HookRunner`,
  with **post-event** semantics: the run already failed, so a raising hook is
  surfaced (logged), not fatal — like ``post_build``'s post-commit stance.

* **Deferred emitter (seam only)** — ``pre_deploy``. The **subscription
  mechanism is built and tested**; its EMITTER lands with deploy (Incr 6).
  The registry accepts handlers for it **without firing them** — firing
  arrives with the emitter. This is a deliberate seam, not missing
  functionality: the slice bar is "the subscription registers and the runner
  gates/clamps/fail-closes it"; the end-to-end "a ``pre_deploy`` hook blocks a
  deploy" is verified with the deploy emitter at its own increment.
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


# Events whose emitters exist: the loop fires the tool hooks; the build flow
# fires ``post_build`` after a Build is recorded (plan-build Unit 2); the
# runtime worker fires ``on_run_failed`` at its ``run.failed`` transition
# (events slice). A subscription to one of these is live immediately.
EMITTED_EVENTS: frozenset[HookEvent] = frozenset(
    {
        HookEvent.PRE_TOOL,
        HookEvent.POST_TOOL,
        HookEvent.POST_BUILD,
        HookEvent.ON_RUN_FAILED,
    }
)

# Events whose subscription is wired but whose emitter is a later
# increment. Registering a handler here is allowed (and tested); it simply
# never fires until the owning increment emits the event. ``pre_deploy``'s
# emitter is deploy (Incr 6); ``on_run_failed`` left this set in the events
# slice (the runtime worker now fires it).
DEFERRED_EMITTER_EVENTS: frozenset[HookEvent] = frozenset({HookEvent.PRE_DEPLOY})


class DeferredEmitterEvent(RuntimeError):
    """Raised if code tries to *emit* a not-yet-wired event this slice.

    Subscribing to ``pre_deploy`` is fine (the seam). Attempting to fire it
    *now* is a programming error — its emitter belongs to deploy (Incr 6) —
    so :meth:`HookRegistry.emit` refuses it loudly rather than silently
    dropping handlers. (``post_build`` left this set in plan-build Unit 2,
    and ``on_run_failed`` in the events slice: their emitters — the build
    flow and the runtime worker — now exist, so emitting either fires
    handlers like any wired event.)
    """


class HookRegistry:
    """Subscribe :data:`HookHandler` callables per :class:`HookEvent`.

    Handlers are kept in registration order and fired in that order on
    :meth:`emit`. Subscribing to a deferred-emitter event is allowed (the
    seam); *emitting* one this slice raises :class:`DeferredEmitterEvent`.
    """

    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[HookHandler]] = {event: [] for event in HookEvent}

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
