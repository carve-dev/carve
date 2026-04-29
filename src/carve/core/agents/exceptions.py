"""Exceptions for the agent loop.

`AgentError` is the umbrella type. Specific subclasses let callers (and
tests) distinguish between an unexpected stop reason, a turn-limit
overrun, and a rate-limit retry exhaustion. Tool execution failures are
*not* exceptions — they are returned as tool results so the model can
recover; see `tools.py` for the convention.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base class for any agent-loop failure surfaced to the caller."""


class MaxTurnsExceeded(AgentError):
    """Raised when the loop hits its `max_turns` ceiling."""


class UnexpectedStopReason(AgentError):
    """Raised when the SDK returns a `stop_reason` we don't handle."""


class RateLimitExhausted(AgentError):
    """Raised after exponential-backoff retries are all consumed."""


class InvalidRequestError(AgentError):
    """Raised on a non-retryable Anthropic 4xx (bad schema, oversized message)."""
