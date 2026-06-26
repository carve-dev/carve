"""Project memory — the runtime read/edit machinery for the memory files
that `carve init` scaffolds (`conventions.md`, `standards.md`, `decisions.md`,
and pipeline/el sidecars).

This package is the single canonical reader: every consumer that needs memory
goes through :class:`~carve.core.memory.loader.MemoryLoader` rather than
reading the files itself.

Shipped (see DELIVERY): the loader, the task selector, the append-decision +
write-conventions writer, and the `carve memory` CLI — including `carve memory
refresh`, which runs the dbt convention-inference engine
(:mod:`carve.integrations.dbt.conventions`) and persists the inferred
`conventions.md`. Deferred (each tracked): REST + MCP parity, and the
`plan_id`-gated `standards`/sidecar writes (the Plan/Build state model can't yet
express the "built, not deployed" gate).
"""

from __future__ import annotations

from carve.core.memory.loader import MemoryFile, MemoryLoader
from carve.core.memory.selector import MemoryBundle, select_for_task
from carve.core.memory.writer import DecisionAlreadyExists, MemoryWriter

__all__ = [
    "DecisionAlreadyExists",
    "MemoryBundle",
    "MemoryFile",
    "MemoryLoader",
    "MemoryWriter",
    "select_for_task",
]
