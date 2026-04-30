"""Runner protocol: the interface every concrete runner implements.

A runner takes a step plus a `RunContext` and produces a `StepResult`.
`execute()` is non-blocking — it spawns the work and returns a
`RunHandle` immediately; `wait()` blocks until the work finishes.
The split lets the future API server (M2) start a run and return a
200 to the client while the subprocess continues in the background.

Log lines are flushed to the state store via `Repository.append_log`
as they arrive, so `stream_logs()` and the future WebSocket layer can
read from a single source of truth.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from carve.core.steps.base import RunContext, Step, StepResult


class RunHandle(BaseModel):
    """Lightweight reference returned by `Runner.execute`.

    Includes the OS process id so callers can correlate state-store
    rows with running processes if they need to.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    process_id: int


class LogLine(BaseModel):
    """A single line emitted by a running step."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    level: str
    source: str
    message: str


class Runner(Protocol):
    """The interface every runner implements."""

    def execute(self, step: Step, context: RunContext) -> RunHandle:
        """Start execution. Returns immediately with a handle."""
        ...

    async def stream_logs(self, run_id: str) -> AsyncIterator[LogLine]:
        """Stream logs as they're produced."""
        ...

    def get_status(self, run_id: str) -> str:
        """Return the current run status."""
        ...

    def cancel(self, run_id: str) -> None:
        """Cancel a running step (SIGTERM, then SIGKILL after a grace period)."""
        ...

    def wait(self, run_id: str) -> StepResult:
        """Block until the step completes; return the result."""
        ...
