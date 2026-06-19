"""The ``todo`` tool + the per-run todo-list state.

A long agent run drifts. The ``todo`` tool is a TodoWrite-style scratch
list the agent rewrites as it works, so it (and the UI / stream) can see
the remaining steps. The list is **harness-owned state**, not part of the
model's context budget beyond the latest write: the agent replaces the
whole list each call, and the harness keeps the current snapshot.

The tool does nothing privileged — it only stores a validated list of
``{content, status}`` items — so it is permitted in every mode and never
gated. It is wired per-run by the runner, which constructs a fresh
:class:`TodoList` and reads ``list.items`` back for surfacing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult

_VALID_STATUS = frozenset({"pending", "in_progress", "completed"})


@dataclass
class TodoItem:
    """One todo entry: a short ``content`` line and a ``status``."""

    content: str
    status: str = "pending"


@dataclass
class TodoList:
    """The current todo snapshot for one run.

    The ``todo`` tool overwrites ``items`` wholesale each call (matching
    the TodoWrite contract); callers read ``items`` to surface progress.
    """

    items: list[TodoItem] = field(default_factory=list)


TODO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": (
                "The complete current todo list (replaces the previous "
                "list). Order matters: top to bottom is execution order."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": sorted(_VALID_STATUS),
                    },
                },
                "required": ["content", "status"],
            },
        },
    },
    "required": ["todos"],
}


def make_todo_tool(todo_list: TodoList) -> Tool:
    """Build a ``todo`` tool that overwrites ``todo_list`` each call."""

    def _execute(input_: ToolInput) -> ToolResult:
        raw = input_.get("todos")
        if not isinstance(raw, list):
            raise ToolExecutionError("`todos` must be an array.")
        items: list[TodoItem] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise ToolExecutionError("each todo must be an object.")
            content = entry.get("content")
            status = entry.get("status", "pending")
            if not isinstance(content, str) or not content.strip():
                raise ToolExecutionError("each todo needs a non-empty `content`.")
            if status not in _VALID_STATUS:
                raise ToolExecutionError(
                    f"invalid status {status!r}; one of {sorted(_VALID_STATUS)}."
                )
            items.append(TodoItem(content=content, status=status))
        todo_list.items = items
        completed = sum(1 for i in items if i.status == "completed")
        return {"count": len(items), "completed": completed}

    return Tool(
        name="todo",
        description=(
            "Maintain your task list across a long run. Call with the "
            "COMPLETE current list each time (it replaces the previous "
            "one). Mark items pending / in_progress / completed so the "
            "user can follow along."
        ),
        input_schema=TODO_SCHEMA,
        executor=_execute,
    )


__all__ = ["TODO_SCHEMA", "TodoItem", "TodoList", "make_todo_tool"]
