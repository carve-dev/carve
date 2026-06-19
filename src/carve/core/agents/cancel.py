"""The cancellation signal the loop checks between turns.

A run is cancelled cooperatively: a user/API cancel calls
:meth:`CancellationToken.cancel`, and the loop — which checks
:meth:`CancellationToken.cancelled` between turns — stops cleanly at the
next boundary and reports a cancelled outcome (the ``run.cancelled``
event payload itself belongs to the events spec; this module owns the
*signal* and the marker the loop raises).

This is intentionally thread-safe but dead simple: the async
``carve serve`` runs the sync loop in a threadpool and flips the token
from the request thread; the loop polls it. In-flight subprocesses are
killed by the shipped runner's process-group SIGTERM→SIGKILL, not here.
"""

from __future__ import annotations

import threading

from carve.core.agents.exceptions import AgentError


class RunCancelled(AgentError):
    """Raised inside the loop when the cancellation token is tripped.

    A subclass of ``AgentError`` so existing ``except AgentError`` sites
    treat a cancel as a clean agent-level stop, not a crash. The loop
    raises this between turns; callers translate it into the
    ``run.cancelled`` event / status.
    """


class CancellationToken:
    """A thread-safe one-shot cancel flag.

    ``cancel()`` is idempotent; ``cancelled`` reads the flag. The loop
    calls ``raise_if_cancelled()`` between turns to bail at a safe point.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation. Idempotent."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`RunCancelled` if a cancel has been requested."""
        if self._event.is_set():
            raise RunCancelled("Run cancelled by user/API between turns.")


__all__ = ["CancellationToken", "RunCancelled"]
