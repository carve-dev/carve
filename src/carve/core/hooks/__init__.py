"""Declarative hooks — config parse, event registry, gated runner, wiring.

A hook is a ``run = "<command>"`` shell action subscribed to a lifecycle
event (``pre_tool`` / ``post_tool`` / ``pre_deploy`` / ``post_build`` /
``on_run_failed``), configured in ``carve/hooks.toml``. Hooks run through
the **same bash gate** as the agent (no bypass), are **mode-clamped**, and
are **fail-closed on error/timeout** (a failing hook blocks the action).

Public surface:

- :class:`HookEvent`, :class:`HookSpec`, :func:`load_hooks_config` — the
  parsed config.
- :class:`HookRegistry` — subscribe handlers per event (incl. the
  not-yet-emitted lifecycle events, which register without firing).
- :class:`HookRunner` — the gated/clamped/fail-closed executor.
- :func:`build_tool_hooks` — the ``pre_tool``/``post_tool`` callables the
  ``AgentLoop`` accepts.
"""

from carve.core.hooks.config import (
    HookConfigError,
    HookSpec,
    load_hooks_config,
    parse_hooks_config,
)
from carve.core.hooks.events import (
    EMITTED_EVENTS,
    DeferredEmitterEvent,
    HookEvent,
    HookRegistry,
)
from carve.core.hooks.runner import HookExecutionError, HookRunner
from carve.core.hooks.wiring import build_tool_hooks

__all__ = [
    "EMITTED_EVENTS",
    "DeferredEmitterEvent",
    "HookConfigError",
    "HookEvent",
    "HookExecutionError",
    "HookRegistry",
    "HookRunner",
    "HookSpec",
    "build_tool_hooks",
    "load_hooks_config",
    "parse_hooks_config",
]
