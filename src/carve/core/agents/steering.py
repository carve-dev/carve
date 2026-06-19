"""The steering queue the chat loop drains between turns.

In an interactive chat session, the user can type guidance while the
agent is mid-task. That guidance is *not* injected into the current
in-flight turn (which is already in the model's hands); instead it is
enqueued and **drained between turns** — appended as a user message
before the next ``messages.create``. Batch ``plan`` / ``build`` runs are
non-interactive and simply never have anything enqueued.

Thread-safe for the same reason as the cancel token: the serve layer
enqueues from the request thread while the sync loop drains from its own
thread.
"""

from __future__ import annotations

import threading


class SteeringQueue:
    """A thread-safe FIFO of mid-task user guidance messages.

    The loop calls :meth:`drain` between turns; each drained string is
    appended to the conversation as a user message before the next API
    call. Empty by default (the batch paths never push).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: list[str] = []

    def push(self, message: str) -> None:
        """Enqueue a guidance message (no-op for empty/whitespace)."""
        if not message or not message.strip():
            return
        with self._lock:
            self._messages.append(message)

    def drain(self) -> list[str]:
        """Return and clear all queued messages (oldest first)."""
        with self._lock:
            drained = self._messages
            self._messages = []
        return drained

    @property
    def pending(self) -> bool:
        with self._lock:
            return bool(self._messages)


__all__ = ["SteeringQueue"]
