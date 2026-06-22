"""Select which memory files belong in a given task's pre-scoped context.

A pure function over a :class:`~carve.core.memory.loader.MemoryLoader`. The
orchestrator calls this after goal classification + impact-context gathering,
before specialist dispatch; the resulting :class:`MemoryBundle` is serialized
into the specialists' context.
"""

from __future__ import annotations

from dataclasses import dataclass

from carve.core.memory.loader import MemoryFile, MemoryLoader


@dataclass(frozen=True)
class MemoryBundle:
    """The memory files selected for one task."""

    conventions: MemoryFile | None
    standards: MemoryFile | None
    decisions: MemoryFile | None  # often None for plan/build; populated for ask
    pipeline_sidecars: dict[str, MemoryFile]  # keyed by pipeline name
    el_sidecars: dict[str, MemoryFile]  # keyed by el artifact name


# Goal classifications for which `decisions.md` is relevant even on a non-
# investigative (plan/build) invocation. Intentionally empty for now and easy
# to tune as the orchestrator's classification vocabulary firms up; the
# `is_investigative` flag (a `carve ask` invocation) is the primary trigger.
_DECISION_RELEVANT_CLASSIFICATIONS: frozenset[str] = frozenset()


def select_for_task(
    *,
    classification: str,
    pipeline_targets: list[str],
    el_targets: list[str],
    is_investigative: bool,
    loader: MemoryLoader,
) -> MemoryBundle:
    """Pick the memory files for a task.

    ``conventions`` and ``standards`` are always included. ``decisions`` is
    included for investigative (`carve ask`) invocations, or when the goal
    classification is decision-relevant. Sidecars are included for every named
    pipeline / el target that actually has one.
    """
    include_decisions = is_investigative or classification in _DECISION_RELEVANT_CLASSIFICATIONS

    pipeline_sidecars: dict[str, MemoryFile] = {}
    for name in pipeline_targets:
        sidecar = loader.load_pipeline_sidecar(name)
        if sidecar is not None:
            pipeline_sidecars[name] = sidecar

    el_sidecars: dict[str, MemoryFile] = {}
    for name in el_targets:
        sidecar = loader.load_el_sidecar(name)
        if sidecar is not None:
            el_sidecars[name] = sidecar

    return MemoryBundle(
        conventions=loader.load_conventions(),
        standards=loader.load_standards(),
        decisions=loader.load_decisions() if include_decisions else None,
        pipeline_sidecars=pipeline_sidecars,
        el_sidecars=el_sidecars,
    )


__all__ = ["MemoryBundle", "select_for_task"]
