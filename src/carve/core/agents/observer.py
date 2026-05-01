"""Observer protocol for `AgentLoop` progress events.

`AgentLoop` runs the Anthropic tool-use loop synchronously, often for
30 seconds to several minutes. Without a hook to surface what's
happening mid-run, the only signal a user gets is that their terminal
appears frozen.

`AgentObserver` is the minimal callback surface the loop calls into at
each interesting event boundary. The default implementation
(`NullObserver`) is a no-op so existing callers keep working
untouched. The CLI wires up a `RichConsoleObserver` (in
`carve.cli.orchestrator.observers`) that turns the events into the
spinner + per-tool-call output described in spec M1.1-04. Future sinks
— a WebSocket bridge, a JSONL file logger — implement the same
protocol with no changes to the loop.

Keep this module dependency-free apart from the standard library:
anything that needs `rich`, `anthropic`, etc. belongs in a concrete
implementation, not the protocol.
"""

from __future__ import annotations

from typing import Any, Protocol


class AgentObserver(Protocol):
    """Receiver for `AgentLoop` progress events.

    Methods are invoked synchronously on the loop's thread, so they
    must return promptly and must not raise. The loop does not catch
    observer exceptions — implementations are responsible for their
    own error containment.
    """

    def on_turn_start(self, turn: int) -> None:
        """Fired before each `messages.create` call. `turn` is 1-based."""
        ...

    def on_tool_call(self, name: str, input: dict[str, Any]) -> None:
        """Fired before invoking a tool executor."""
        ...

    def on_tool_result(
        self,
        name: str,
        ok: bool,
        summary: str,
        duration_ms: int,
    ) -> None:
        """Fired after the tool executor returns or raises.

        `ok` is False when the tool raised (the loop translates that
        to an `is_error=True` tool_result block, but the observer sees
        the original outcome). `summary` is a short caller-friendly
        string — row count, byte count, error message, etc. — computed
        by the loop from the tool's return value.
        """
        ...

    def on_turn_complete(
        self,
        turn: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Fired after each `messages.create` returns and usage is added.

        Token counts are cumulative across the run, matching the
        running totals the CLI eventually displays.
        """
        ...

    def on_done(
        self,
        total_turns: int,
        total_tool_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        """Fired exactly once when the loop reaches `end_turn`."""
        ...


class NullObserver:
    """No-op `AgentObserver`. Default when no observer is supplied."""

    def on_turn_start(self, turn: int) -> None:
        return None

    def on_tool_call(self, name: str, input: dict[str, Any]) -> None:
        return None

    def on_tool_result(
        self,
        name: str,
        ok: bool,
        summary: str,
        duration_ms: int,
    ) -> None:
        return None

    def on_turn_complete(
        self,
        turn: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        return None

    def on_done(
        self,
        total_turns: int,
        total_tool_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        return None


__all__ = ["AgentObserver", "NullObserver"]
