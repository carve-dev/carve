"""Orchestrator-side seams that enrich a task's pre-scoped context.

These hooks are invoked by the orchestrator after classification +
impact-context gathering, before specialist dispatch. The memory hook is
shipped here ahead of its caller: the plan orchestrator doesn't yet produce a
goal classification (that lands with plan-build), so `attach_memory_to_context`
is dormant — unit-tested but not yet wired into a live invocation.
"""

from __future__ import annotations

from carve.core.agents.orchestrator_hooks.memory import attach_memory_to_context

__all__ = ["attach_memory_to_context"]
