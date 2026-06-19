"""Wire declarative hooks into the loop's ``pre_tool``/``post_tool`` seam.

:func:`build_tool_hooks` turns a list of :class:`HookSpec` + a
:class:`HookRunner` into the two ``Hook`` callables ``AgentLoop`` accepts
(``pre_tool_hook`` / ``post_tool_hook``). Each callable, when the loop
fires it for a tool call:

1. selects the specs subscribed to that event whose ``match`` matches the
   call (tool name + ``bash`` command glob),
2. runs each via the :class:`HookRunner` (gated + clamped + fail-closed).

The loop order is **gate → pre_tool → execute → post_tool**
(``loop.py``), so a ``pre_tool`` hook fires **after** the gate has already
admitted the call: it can only **further-restrict** (raise to abort),
never enable a denied call. A :class:`HookExecutionError` propagates out
of the callable; the loop catches a raising hook and turns it into a
tool-call abort.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from fnmatch import fnmatch
from typing import Any

from carve.core.hooks.config import HookSpec
from carve.core.hooks.events import HookEvent
from carve.core.hooks.runner import HookRunner

logger = logging.getLogger(__name__)

# Matches ``loop.Hook``: (tool_name, tool_input) -> None; may raise to abort.
ToolHook = Callable[[str, dict[str, Any]], None]


def build_tool_hooks(
    specs: list[HookSpec], runner: HookRunner
) -> tuple[ToolHook | None, ToolHook | None]:
    """Build ``(pre_tool_hook, post_tool_hook)`` from ``specs``.

    Returns ``None`` for an event with no subscribed specs so the loop can
    skip the call entirely (the loop treats a ``None`` hook as "no hook").
    """
    pre = [s for s in specs if s.event is HookEvent.PRE_TOOL]
    post = [s for s in specs if s.event is HookEvent.POST_TOOL]
    pre_hook = _make_hook(pre, runner) if pre else None
    post_hook = _make_hook(post, runner) if post else None
    return pre_hook, post_hook


def _make_hook(specs: list[HookSpec], runner: HookRunner) -> ToolHook:
    def _hook(tool_name: str, tool_input: dict[str, Any]) -> None:
        for spec in specs:
            if _matches(spec, tool_name, tool_input):
                command = _expand(spec.run, tool_name, tool_input)
                # Fail-closed: HookExecutionError propagates → loop aborts
                # the tool call. We do NOT swallow it.
                runner.run(spec, command=command)

    return _hook


def _matches(spec: HookSpec, tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Return True iff ``spec``'s ``match`` filter matches this call."""
    if spec.match.tool is not None and spec.match.tool != tool_name:
        return False
    if spec.match.command is not None:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return False
        if not fnmatch(command, spec.match.command):
            return False
    return True


def _expand(template: str, tool_name: str, tool_input: dict[str, Any]) -> str:
    """Substitute ``{tool}`` / ``{command}`` placeholders in a hook command.

    A minimal, explicit substitution (not arbitrary ``str.format`` over
    attacker-influenced keys): only the two known placeholders are
    replaced, and the result still passes through the bash gate, so an
    expanded value carrying a metacharacter is denied there — the
    expansion can't smuggle a second command past the gate.
    """
    command = tool_input.get("command")
    out = template.replace("{tool}", tool_name)
    if isinstance(command, str):
        out = out.replace("{command}", command)
    return out


__all__ = ["ToolHook", "build_tool_hooks"]
