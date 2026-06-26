"""Generic tool primitives consumed by the agent loop.

A `Tool` is a name, description, JSON-schema input contract, and a
synchronous executor that takes a parsed input dict and returns a JSON-
serializable result. Executors raise `ToolExecutionError` (or any
exception, really) to signal a failure that the loop will translate
into a tool result with `is_error=True` rather than crashing.

The schema dict produced by `to_schema()` matches Anthropic's
`tools=[...]` payload exactly.

This module also serves as the namespace package for the harness tool
factories (``bash_tool``, ``fs_tools``, ``search_tools``, ``web_tools``,
``todo_tool``, …), each a sibling submodule. The base primitives are
re-exported at the package root so existing imports from
`carve.core.agents.tools` continue to resolve unchanged after the
conversion from a flat module to a package.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ToolInput = dict[str, Any]
ToolResult = str | dict[str, Any] | list[Any]
ToolExecutor = Callable[[ToolInput], ToolResult]


class ToolExecutionError(Exception):
    """Raised by a tool executor when input is bad or work cannot proceed.

    The loop catches this (and any other exception) and turns it into a
    tool result with ``is_error=True``. The agent sees the message and
    can recover — the loop never crashes on a tool failure.
    """


@dataclass(frozen=True)
class Tool:
    """A single tool the agent can call.

    Attributes:
        name: Unique tool name. Must match across the schema and the
            tool-use response from the model.
        description: One- to three-sentence prose for the model.
        input_schema: JSON schema describing valid inputs. The Anthropic
            API enforces this on the model's behalf.
        executor: Callable that performs the tool's work. Raises on
            failure; the loop turns the exception into a tool error.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    executor: ToolExecutor

    def to_schema(self) -> dict[str, Any]:
        """Serialize for the Anthropic `tools` parameter."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


__all__ = [
    "Tool",
    "ToolExecutionError",
    "ToolExecutor",
    "ToolInput",
    "ToolResult",
]
