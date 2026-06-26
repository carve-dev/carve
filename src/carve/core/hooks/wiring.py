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

:func:`build_post_build_hook` is the **lifecycle** analogue for
``post_build``. A lifecycle hook takes the event payload (no tool/command)
rather than ``(tool_name, tool_input)``, expands the payload keys into the
command, and runs each matching spec through the *same* gated
:class:`HookRunner`. The difference from a tool hook is **when** it fires
and what a raise means: ``post_build`` fires **after** the ``Build`` row is
durably recorded, so the build flow treats a raising hook as
surfaced-not-rolled-back (post-commit), not an abort — the gate still
clamps/denies the command exactly as for a tool hook, but the Build stands.
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

# A lifecycle hook: (payload) -> None; may raise (a HookExecutionError). The
# caller decides what a raise means — for ``post_build`` it is post-commit,
# so the build flow surfaces a raise but does NOT roll the Build back.
LifecycleHook = Callable[[dict[str, Any]], None]


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


def build_post_build_hook(specs: list[HookSpec], runner: HookRunner) -> LifecycleHook | None:
    """Build the single ``post_build`` lifecycle hook from ``specs``.

    Filters ``specs`` for :attr:`HookEvent.POST_BUILD` and returns a
    callable ``(payload) -> None`` that runs each matching spec's command
    (its ``{placeholders}`` expanded from the payload) through the same
    gated :class:`HookRunner` a tool hook uses. Returns ``None`` when no
    spec subscribes to ``post_build`` so the build flow can skip the call.

    A ``post_build`` spec's ``match`` filter is **inert** — there is no
    tool/command to match against a lifecycle event, so a ``match`` on a
    ``post_build`` hook is ignored rather than rejected (a hook with no
    ``match`` is the common, intended case). A :class:`HookExecutionError`
    propagates out of the callable (a denied / non-zero / timed-out
    command); the build flow decides how to surface it — for ``post_build``
    that is post-commit (logged, the Build stands), not a roll-back.
    """
    post = [s for s in specs if s.event is HookEvent.POST_BUILD]
    if not post:
        return None

    def _hook(payload: dict[str, Any]) -> None:
        for spec in post:
            command = _expand_lifecycle(spec.run, payload)
            # Same gated runner as tool hooks: a denied / non-zero command
            # raises HookExecutionError. Unlike a pre-action tool hook, the
            # build flow treats that raise as post-commit (surfaced, the
            # Build stands) — but the gate clamp/deny is identical.
            runner.run(spec, command=command)

    return _hook


def _expand_lifecycle(template: str, payload: dict[str, Any]) -> str:
    """Substitute lifecycle ``{key}`` placeholders from the event payload.

    The lifecycle analogue of :func:`_expand`: only the payload's own keys
    are substituted (``{pipeline_name}`` / ``{build_id}`` / ``{target}`` /
    ``{plan_id}`` / ``{files}`` for ``post_build``). A list/tuple value (e.g.
    ``{files}``) renders **space-joined** — the natural shell form
    (``el/a.py el/b.py``), not a Python list repr (``['el/a.py']``) a hook
    author would never want in a command. Scalars render via ``str``. This is
    an explicit per-key replace, not arbitrary ``str.format`` over attacker
    keys, and the expanded command still passes through the bash gate — an
    expanded value carrying a metacharacter is denied there, so the expansion
    can't smuggle a second command past the gate.
    """
    out = template
    for key, value in payload.items():
        placeholder = "{" + key + "}"
        if placeholder in out:
            rendered = (
                " ".join(str(item) for item in value)
                if isinstance(value, list | tuple)
                else str(value)
            )
            out = out.replace(placeholder, rendered)
    return out


__all__ = [
    "LifecycleHook",
    "ToolHook",
    "build_post_build_hook",
    "build_tool_hooks",
]
