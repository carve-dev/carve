"""Context compaction for the top-level interactive chat loop.

A long chat eventually approaches the model's context window. Compaction
keeps it in budget: when the running token count crosses a threshold
(default ~75% of the window), the oldest conversation turns are replaced
by a single summary message, while the system prompt, the active task,
and the most recent turns are kept verbatim.

This applies to the **interactive chat loop only**. Subagents and the
batch ``plan`` / ``build`` paths are bounded by ``max_turns`` and never
compact — so compaction lives here as a helper the chat entry calls
between turns, not inside ``AgentLoop.run`` (which the batch paths share
and which must stay a faithful, un-summarized transcript).

The summarizer is injected (a callable that turns a slice of messages
into one summary string), so this module needs no Anthropic dependency
and is unit-testable with a stub.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# A summarizer takes the messages being dropped and returns prose that
# stands in for them. The chat entry passes a small LLM call; tests pass
# a deterministic stub.
Summarizer = Callable[[list[dict[str, Any]]], str]

# Keep this many of the most-recent messages verbatim; only older ones
# are eligible for summarization.
_KEEP_RECENT = 6


def should_compact(
    token_count: int,
    *,
    context_window: int,
    threshold: float = 0.75,
) -> bool:
    """Return True when ``token_count`` crosses the compaction threshold."""
    if context_window <= 0:
        return False
    return token_count >= int(context_window * threshold)


def compact_messages(
    messages: list[dict[str, Any]],
    *,
    summarizer: Summarizer,
    keep_recent: int = _KEEP_RECENT,
) -> list[dict[str, Any]]:
    """Replace the oldest turns of ``messages`` with one summary message.

    The system prompt is **not** part of ``messages`` (the loop holds it
    separately), so the whole list here is the conversation. We keep the
    last ``keep_recent`` messages, summarize everything older into a
    single ``user`` message, and prepend that summary so the model still
    has the gist of the early conversation.

    If there is nothing old enough to compact (``len <= keep_recent``)
    the list is returned unchanged.
    """
    if len(messages) <= keep_recent:
        return list(messages)

    head = messages[:-keep_recent]
    tail = messages[-keep_recent:]
    summary_text = summarizer(head)
    summary_message: dict[str, Any] = {
        "role": "user",
        "content": (f"[Earlier conversation summarized to stay within context]\n{summary_text}"),
    }
    return [summary_message, *tail]


__all__ = ["Summarizer", "compact_messages", "should_compact"]
