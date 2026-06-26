"""Decompose a natural-language goal into an ordered list of routed sub-goals.

Sub-slice A (#44) maps a goal to **exactly one** classification and runs one
engineer. Sub-slice B generalizes that to **several** engineers for one goal:
"ingest the Stripe API, then stage it with dbt" is two sub-goals — a
``new_pipeline`` for the dlt-engineer and a ``new_model`` for the dbt-engineer.
This module is the decomposition step; the planner runs each sub-goal through
the same single-engine machinery and merges the N designs into one Plan.

**Mechanism.** A single one-shot Anthropic call — the constrained-completion
twin of :func:`carve.cli.orchestrator.goal_classifier.classify_goal`, NOT a full
``AgentLoop`` turn (decomposition is deterministic dispatch, not an agentic
loop). The model is forced — via ``tool_choice`` over a one-tool schema whose
``classification`` is an ``enum`` of exactly the **live candidate set** (reused
from :func:`candidate_classifications`, the single source of truth shared with
the classifier so the two never drift) — to return an **ordered** array of
``{sub_goal, classification}`` objects. We re-validate every returned
classification against the candidate set (defense in depth, mirroring the
classifier) and raise :class:`GoalDecompositionError` on a no-tool-call / empty
list / out-of-set / empty-``sub_goal`` answer rather than silently routing.

A **single-step goal yields a 1-element decomposition**, so the single-engine
route (#44) is preserved as the N=1 case through this same path.

**Offline-testable.** The Anthropic ``client`` is injected (the same seam the
classifier uses). A stub client whose ``messages.create`` returns a canned
multi-step tool-use response yields a deterministic decomposition with no
network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from carve.cli.orchestrator.goal_classifier import candidate_classifications
from carve.core.agents.exceptions import AgentError
from carve.core.agents.subagent_registry import SubagentRegistry

logger = logging.getLogger(__name__)

# The tool the model is forced to call, returning the ordered sub-goal array. A
# single structured call (tool_choice-pinned) is the constrained-completion form
# the spec calls for — not an agentic loop.
_DECOMPOSE_TOOL_NAME = "decompose_goal"


class GoalDecompositionError(AgentError):
    """The goal could not be decomposed into a routable list of sub-goals.

    Sibling of :class:`~carve.cli.orchestrator.goal_classifier.GoalClassificationError`
    in intent: a *clear* failure to decompose lets the planner fall back to the
    monolithic M1 path (an undecomposable goal still works) rather than route to
    a wrong — or partial — set of engines. Raised when the model returns no tool
    call, an empty list, a sub-goal with an empty ``sub_goal`` string, or a
    ``classification`` outside the live registry's candidate set.
    """


@dataclass(frozen=True)
class SubGoal:
    """One step of a decomposed goal: a slice of work and its routed label.

    ``sub_goal`` is the natural-language slice handed to the engineer as the
    delegated task; ``classification`` is the registered label
    :func:`carve.core.agents.routing.select_agent` resolves to an engineer.
    Frozen so a decomposition is an immutable ordered record.
    """

    sub_goal: str
    classification: str


def decompose_goal(
    goal: str,
    *,
    client: Any,
    model: str,
    registry: SubagentRegistry,
    max_tokens: int = 1024,
) -> list[SubGoal]:
    """Return the ordered list of routed sub-goals for ``goal``.

    Args:
        goal: The user's natural-language goal.
        client: A resolved Anthropic client (the planner passes
            ``make_client(config, client)``). Injected so a stub returning a
            canned decomposition makes this offline-testable.
        model: The model id to decompose with (the planner passes
            ``config.models.default_model``).
        registry: The live :class:`SubagentRegistry`; the candidate label set is
            its agents' union of ``classifications`` (via
            :func:`candidate_classifications` — the same source the classifier
            and router use, so the labels never drift).
        max_tokens: Cap on the decomposition response (a small ordered list).

    Returns:
        An ordered ``list[SubGoal]``; a single-step goal yields a 1-element list.

    Raises:
        GoalDecompositionError: No agent declares any classification, the goal
            is empty, or the model's answer is missing / empty / carries an
            empty sub-goal / uses a classification outside the candidate set.
    """
    if not goal or not goal.strip():
        raise GoalDecompositionError("Cannot decompose an empty goal.")

    candidates = candidate_classifications(registry)
    if not candidates:
        # No declarative engine declares a classification: there is nothing to
        # route to. Surface it so the planner falls back to the M1 path rather
        # than calling the model pointlessly (mirrors the classifier).
        raise GoalDecompositionError(
            "No registered agent declares any classification; nothing to route on."
        )

    tool = _decompose_tool_schema(candidates)
    response = client.messages.create(
        model=model,
        system=_DECOMPOSE_SYSTEM_PROMPT,
        max_tokens=max_tokens,
        tools=[tool],
        tool_choice={"type": "tool", "name": _DECOMPOSE_TOOL_NAME},
        messages=[{"role": "user", "content": _user_message(goal, candidates)}],
    )

    sub_goals = _extract_sub_goals(response)
    if not sub_goals:
        raise GoalDecompositionError(
            "The decomposer returned no sub-goals for the goal "
            f"{_truncate(goal)!r}; cannot route to any engine."
        )
    for sub in sub_goals:
        if sub.classification not in candidates:
            raise GoalDecompositionError(
                f"The decomposer returned classification {sub.classification!r}, "
                f"which is not a registered classification. Known: {candidates}."
            )
    logger.debug(
        "Decomposed goal %r into %d sub-goal(s): %s.",
        _truncate(goal),
        len(sub_goals),
        [s.classification for s in sub_goals],
    )
    return sub_goals


# ---------------------------------------------------------------------------
# Prompt + schema
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM_PROMPT = (
    "You are a planning decomposer for a data-engineering agent. Given a "
    "natural-language goal, break it into the ORDERED list of sub-goals needed "
    "to accomplish it, and for each sub-goal pick the single best classification "
    "label from the allowed set. Return the list by calling the decompose_goal "
    "tool exactly once. A simple goal that one engine handles is a single "
    "sub-goal. Preserve the order the work must happen in; never invent a label "
    "outside the allowed set."
)


def _user_message(goal: str, candidates: list[str]) -> str:
    options = "\n".join(f"- {label}" for label in candidates)
    return (
        f"Goal:\n{goal.strip()}\n\n"
        f"Allowed classification labels:\n{options}\n\n"
        "Call decompose_goal with the ordered list of sub-goals; each sub_goal "
        "is the slice of work and each classification is its best-matching label."
    )


def _decompose_tool_schema(candidates: list[str]) -> dict[str, Any]:
    """The one-tool schema whose items' ``classification`` enums the candidates.

    Pinning the per-item enum to the live candidate set is the primary
    constraint (the API enforces it on the model's behalf);
    :func:`decompose_goal` re-validates each returned classification as defense
    in depth.
    """
    return {
        "name": _DECOMPOSE_TOOL_NAME,
        "description": (
            "Return the ordered list of sub-goals for the goal, each with its "
            "best-matching classification label."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sub_goals": {
                    "type": "array",
                    "minItems": 1,
                    "description": ("The ordered sub-goals; a single-engine goal is one item."),
                    "items": {
                        "type": "object",
                        "properties": {
                            "sub_goal": {
                                "type": "string",
                                "description": "The natural-language slice of work.",
                            },
                            "classification": {
                                "type": "string",
                                "enum": candidates,
                                "description": (
                                    "The single best-matching classification label "
                                    "for this sub-goal."
                                ),
                            },
                        },
                        "required": ["sub_goal", "classification"],
                    },
                },
            },
            "required": ["sub_goals"],
        },
    }


def _extract_sub_goals(response: Any) -> list[SubGoal]:
    """Pull the ordered sub-goals from the forced ``decompose_goal`` tool call.

    Reads the same response shape ``AgentLoop`` reads (``response.content`` of
    typed blocks). Returns an empty list when no ``decompose_goal`` tool call
    carries a usable ``sub_goals`` array, so the caller raises a clear
    decomposition failure. An item with a missing/empty ``sub_goal`` or
    ``classification`` is rejected (an empty list signals the failure) rather
    than silently dropped — a partial decomposition would route incompletely.
    """
    content = getattr(response, "content", None)
    if not content:
        return []
    for block in content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", "") != _DECOMPOSE_TOOL_NAME:
            continue
        tool_input = getattr(block, "input", {}) or {}
        if not isinstance(tool_input, dict):
            continue
        raw_items = tool_input.get("sub_goals")
        if not isinstance(raw_items, list) or not raw_items:
            return []
        sub_goals: list[SubGoal] = []
        for item in raw_items:
            if not isinstance(item, dict):
                return []
            sub_goal = item.get("sub_goal")
            classification = item.get("classification")
            if not isinstance(sub_goal, str) or not sub_goal.strip():
                return []
            if not isinstance(classification, str) or not classification.strip():
                return []
            sub_goals.append(
                SubGoal(sub_goal=sub_goal.strip(), classification=classification.strip())
            )
        return sub_goals
    return []


def _truncate(text: str, limit: int = 120) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "GoalDecompositionError",
    "SubGoal",
    "decompose_goal",
]
