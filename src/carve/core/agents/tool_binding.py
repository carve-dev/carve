"""Bind a declarative agent's ``tools:`` grant names to real executors.

A declarative agent file declares *which* tools it may use as a list of
**names** (`tools: [edit, grep, bash, sql, …]`). The registry turns those into
name-only stubs whose executor raises — they declare the grant but cannot run.
This module is the missing seam: given a runtime :class:`BindingContext`
(project dir, the run's gate, an approver, optional skills dir, plus any
caller-injected tools), it maps each grant name to the real harness tool that
performs the work. Names whose dependency the harness can't supply (e.g. `sql`,
which needs a warehouse connection the runner doesn't hold) are injected by the
caller via ``extra_tools``; an unbindable name keeps the raising stub, so an
unbound call still fails loud rather than silently no-opping.

This is what makes any declarative subagent (the shipped `sql-specialist`, the
forthcoming engineers) actually *do* something when delegated to.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from carve.core.agents.m1_tools import make_read_file_tool
from carve.core.agents.permissions.gate import Approver, PermissionGate
from carve.core.agents.subagent_registry import grant_stub_tool, is_grant_stub
from carve.core.agents.tools import Tool
from carve.core.agents.tools.bash_tool import make_bash_tool
from carve.core.agents.tools.fs_tools import make_create_file_tool, make_edit_tool
from carve.core.agents.tools.search_tools import make_glob_tool, make_grep_tool
from carve.core.agents.tools.todo_tool import TodoList, make_todo_tool
from carve.core.agents.tools.web_tools import make_web_fetch_tool, make_web_search_tool
from carve.core.skills.pack_discovery import discover_pack_roots


@dataclass(frozen=True)
class BindingContext:
    """The runtime collaborators a grant name binds against.

    ``project_dir`` roots the file/search/bash tools; ``gate`` (built at the
    child's clamped mode) gates bash; ``approver`` handles interactive prompts;
    ``skills_dir`` enables ``lookup_skill_pack``; ``extra_tools`` supplies tools
    whose dependency lives outside the harness (e.g. a ``sql`` tool built with
    a real warehouse connection, or ``delegate``).
    """

    project_dir: Path
    gate: PermissionGate
    approver: Approver | None = None
    skills_dir: Path | None = None
    extra_tools: Mapping[str, Tool] = field(default_factory=dict)


# Grant name -> builder over the BindingContext, for the harness base tools the
# binder can construct from (project_dir, gate, approver, skills_dir) alone.
_BASE_BUILDERS: dict[str, Callable[[BindingContext], Tool]] = {
    "read_file": lambda c: make_read_file_tool(c.project_dir),
    "grep": lambda c: make_grep_tool(c.project_dir),
    "glob": lambda c: make_glob_tool(c.project_dir),
    "edit": lambda c: make_edit_tool(c.project_dir),
    "create_file": lambda c: make_create_file_tool(c.project_dir),
    "bash": lambda c: make_bash_tool(c.project_dir, gate=c.gate, approver=c.approver),
    "web_fetch": lambda c: make_web_fetch_tool(),
    "web_search": lambda c: make_web_search_tool(),
    "todo": lambda c: make_todo_tool(TodoList()),
}


def _bind_one(name: str, ctx: BindingContext) -> Tool:
    # Caller-injected tools win (they carry deps the harness can't supply).
    if name in ctx.extra_tools:
        injected = ctx.extra_tools[name]
        # Hard precondition: the injected tool must bind under its own name, so
        # the bound name always equals the granted name the gate authorized. A
        # mismatch would otherwise silently break (the grant has no dispatch
        # entry; the differently-named tool is gate-denied) — fail loud instead.
        if injected.name != name:
            raise ValueError(
                f"Injected tool for grant {name!r} has mismatched name "
                f"{injected.name!r}; an extra_tools entry must bind under its grant name."
            )
        return injected
    builder = _BASE_BUILDERS.get(name)
    if builder is not None:
        return builder(ctx)
    if name == "lookup_skill_pack" and ctx.skills_dir is not None:
        return discover_pack_roots(skills_dir=ctx.skills_dir).make_lookup_tool()
    # Unbindable here (e.g. `sql`/`run_snowflake_query` with no injected tool,
    # `mcp:*`, the forthcoming `dlt_library`): keep the raising stub so the
    # grant is still surfaced to the policy intersection and an unbound call
    # fails loud instead of silently no-opping.
    return grant_stub_tool(name)


def bind_grant_tools(tools: Iterable[Tool], ctx: BindingContext) -> list[Tool]:
    """Bind name-only grant **stubs** to real executors; pass real tools through.

    A spec's ``tool_factory`` yields name-only stubs for a declarative agent
    (bind those) but may yield already-real tools for a hand-built spec or a
    test fixture (leave those untouched). De-duplicated by name,
    order-preserving.
    """
    bound: list[Tool] = []
    seen: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            continue
        seen.add(tool.name)
        bound.append(_bind_one(tool.name, ctx) if is_grant_stub(tool) else tool)
    return bound


__all__ = ["BindingContext", "bind_grant_tools"]
