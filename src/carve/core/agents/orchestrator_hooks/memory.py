"""Attach the selected memory bundle to a task's pre-scoped context.

Dormant in the lean core: the function is complete and unit-tested, but the
plan orchestrator doesn't yet produce the goal ``classification`` /
pipeline+el targets it needs (that lands with plan-build). Shipping it now
keeps :class:`~carve.core.memory.loader.MemoryLoader` the single memory entry
point the memory spec mandates; only the live wiring is deferred.
"""

from __future__ import annotations

from typing import Any

from carve.core.memory.loader import MemoryFile, MemoryLoader
from carve.core.memory.selector import select_for_task


def attach_memory_to_context(
    task_context: dict[str, Any],
    *,
    classification: str,
    pipeline_targets: list[str],
    el_targets: list[str],
    is_investigative: bool,
    loader: MemoryLoader,
) -> dict[str, Any]:
    """Return ``task_context`` with a ``"memory"`` block attached.

    The input dict is not mutated — a shallow copy is returned with the
    selected bundle serialized to plain strings/dicts for the agent context.
    """
    bundle = select_for_task(
        classification=classification,
        pipeline_targets=pipeline_targets,
        el_targets=el_targets,
        is_investigative=is_investigative,
        loader=loader,
    )
    enriched = dict(task_context)
    enriched["memory"] = {
        "conventions": _slice_or_full(bundle.conventions, classification),
        "standards": bundle.standards.contents if bundle.standards else None,
        "decisions": bundle.decisions.contents if bundle.decisions else None,
        "pipeline_notes": {n: f.contents for n, f in bundle.pipeline_sidecars.items()},
        "el_notes": {n: f.contents for n, f in bundle.el_sidecars.items()},
    }
    return enriched


def _slice_or_full(conventions: MemoryFile | None, classification: str) -> str | None:
    """Return the full conventions document (a future seam for slicing).

    Conventions are not large, so the whole document is passed through. A later
    orchestrator could slice to the sections relevant to ``classification``
    (e.g. dbt-only sections for a dbt goal); this is the place to do it.
    """
    return conventions.contents if conventions else None


__all__ = ["attach_memory_to_context"]
