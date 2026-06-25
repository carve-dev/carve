"""Classify a natural-language goal to one registered agent classification.

The live single-engine route (sub-slice A of the plan-build keystone) starts
by mapping the user's goal text to **exactly one** classification label, which
:func:`carve.core.agents.routing.select_agent` then resolves to an engineer.
The candidate label set is derived from the **live registry** — the union of
every registered agent's ``classifications`` — so it never drifts from the
agents on disk: add a classification to an engineer's frontmatter and it
becomes classifiable here with no code change.

**Mechanism.** A single one-shot Anthropic call (NOT a full ``AgentLoop`` turn:
classification is deterministic dispatch, not an agentic loop). The model is
given the candidate labels and forced — via ``tool_choice`` over a one-tool
schema whose ``label`` is an ``enum`` of exactly those candidates — to return
one of them. We then re-validate the returned label against the candidate set
(defense in depth: a model that ignores the enum, or an SDK that doesn't
enforce it, must not route us to a wrong engine) and raise
:class:`GoalClassificationError` on an out-of-set / empty / no-tool-call answer
rather than silently defaulting. This mirrors ``routing.py``'s contract: a
clear no-match beats a wrong agent.

**Offline-testable.** The Anthropic ``client`` is injected (the same seam
``generate_plan(client=...)`` exposes). A stub client whose ``messages.create``
returns a canned single-label tool-use response yields a deterministic
classification with no network.
"""

from __future__ import annotations

import logging
from typing import Any

from carve.core.agents.exceptions import AgentError
from carve.core.agents.subagent_registry import SubagentRegistry

logger = logging.getLogger(__name__)

# The tool the model is forced to call, returning exactly one label. A single
# structured call (tool_choice-pinned) is the constrained-completion form the
# spec calls for — not an agentic loop.
_CLASSIFY_TOOL_NAME = "classify_goal"


class GoalClassificationError(AgentError):
    """The goal could not be classified to a registered label.

    A *clear* no-classification — sibling of ``PlanGenerationError`` in intent:
    surfacing it lets the caller fall back to the monolithic M1 path (an
    unclassifiable goal still works) rather than route to a wrong engine.
    Raised when the model returns no tool call, an empty label, or a label
    outside the live registry's candidate set.
    """


def candidate_classifications(registry: SubagentRegistry) -> list[str]:
    """The sorted union of every registered agent's ``classifications``.

    Built off the live registry so the candidate set is exactly the labels the
    agents on disk handle — adding a classification to an engineer's
    frontmatter extends this with no code change. De-duplicated and sorted for
    a stable prompt/schema (the enum order is deterministic across runs).
    """
    labels: set[str] = set()
    for spec in registry.specs():
        labels.update(spec.classifications)
    return sorted(labels)


def classify_goal(
    goal: str,
    *,
    client: Any,
    model: str,
    registry: SubagentRegistry,
    max_tokens: int = 256,
) -> str:
    """Return the one registered classification label for ``goal``.

    Args:
        goal: The user's natural-language goal.
        client: A resolved Anthropic client (the planner passes
            ``make_client(config, client)``). Injected so a stub returning a
            canned label makes this offline-testable.
        model: The model id to classify with (the planner passes
            ``config.models.default_model``).
        registry: The live :class:`SubagentRegistry`; the candidate label set is
            its agents' union of ``classifications``. Threading the *same*
            registry the router uses keeps the candidate set and the route
            resolving against one source of truth.
        max_tokens: Cap on the (tiny) classification response.

    Raises:
        GoalClassificationError: No agent declares any classification, the
            goal is empty, or the model's answer is missing / empty / outside
            the candidate set.
    """
    if not goal or not goal.strip():
        raise GoalClassificationError("Cannot classify an empty goal.")

    candidates = candidate_classifications(registry)
    if not candidates:
        # No declarative engine declares a classification (e.g. an empty
        # builtin/ set): there is nothing to route to. Surface it so the caller
        # falls back to the M1 path rather than calling the model pointlessly.
        raise GoalClassificationError(
            "No registered agent declares any classification; nothing to route on."
        )

    tool = _classify_tool_schema(candidates)
    response = client.messages.create(
        model=model,
        system=_CLASSIFY_SYSTEM_PROMPT,
        max_tokens=max_tokens,
        tools=[tool],
        tool_choice={"type": "tool", "name": _CLASSIFY_TOOL_NAME},
        messages=[{"role": "user", "content": _user_message(goal, candidates)}],
    )

    label = _extract_label(response)
    if label is None:
        raise GoalClassificationError(
            "The classifier returned no label for the goal "
            f"{_truncate(goal)!r}; cannot route to an engine."
        )
    if label not in candidates:
        raise GoalClassificationError(
            f"The classifier returned {label!r}, which is not a registered "
            f"classification. Known: {candidates}."
        )
    logger.debug("Classified goal %r as %r.", _truncate(goal), label)
    return label


# ---------------------------------------------------------------------------
# Prompt + schema
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM_PROMPT = (
    "You are a routing classifier for a data-engineering agent. Given a "
    "natural-language goal, pick the single best classification label from the "
    "allowed set and return it by calling the classify_goal tool exactly once. "
    "Choose the closest match; never invent a label outside the allowed set."
)


def _user_message(goal: str, candidates: list[str]) -> str:
    options = "\n".join(f"- {label}" for label in candidates)
    return (
        f"Goal:\n{goal.strip()}\n\n"
        f"Allowed classification labels:\n{options}\n\n"
        "Call classify_goal with the single best-matching label."
    )


def _classify_tool_schema(candidates: list[str]) -> dict[str, Any]:
    """The one-tool schema whose ``label`` is an enum of the candidates.

    Pinning the enum to the live candidate set is the primary constraint (the
    API enforces it on the model's behalf); :func:`classify_goal` re-validates
    the returned label as defense in depth.
    """
    return {
        "name": _CLASSIFY_TOOL_NAME,
        "description": "Return the single classification label for the goal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "enum": candidates,
                    "description": "The single best-matching classification label.",
                },
            },
            "required": ["label"],
        },
    }


def _extract_label(response: Any) -> str | None:
    """Pull the ``label`` from the forced ``classify_goal`` tool-use block.

    Reads the same response shape ``AgentLoop`` reads (``response.content`` of
    typed blocks). Returns ``None`` when no ``classify_goal`` tool call carries
    a non-empty string label, so the caller raises a clear no-classification.
    """
    content = getattr(response, "content", None)
    if not content:
        return None
    for block in content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", "") != _CLASSIFY_TOOL_NAME:
            continue
        tool_input = getattr(block, "input", {}) or {}
        if not isinstance(tool_input, dict):
            continue
        label = tool_input.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
    return None


def _truncate(text: str, limit: int = 120) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "GoalClassificationError",
    "candidate_classifications",
    "classify_goal",
]
