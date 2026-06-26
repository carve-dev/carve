"""Unit tests for `cli.orchestrator.goal_decomposer.decompose_goal`.

The decomposer is a one-shot LLM call: it builds the candidate label set from
the **live registry** (reusing `candidate_classifications`), forces a single
`decompose_goal` tool call returning an ORDERED `sub_goals` array, and
re-validates every returned classification against the candidate set. A stub
client returning a canned multi-step tool-use response makes it deterministic
and offline. A single-step goal yields a 1-element decomposition (so the #44
single-engine route is preserved as N=1). An out-of-set classification / empty
list / no-tool-call answer raises `GoalDecompositionError` rather than routing
to a wrong — or partial — set of engines.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from carve.cli.orchestrator.goal_classifier import candidate_classifications
from carve.cli.orchestrator.goal_decomposer import (
    GoalDecompositionError,
    SubGoal,
    decompose_goal,
)
from carve.core.agents.discovery import AgentDiscovery
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.subagent_registry import AgentSpec, SubagentRegistry

# --------------------------------------------------------------------- helpers


def _tool_use_block(name: str, input_: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id="tu_1", name=name, input=input_)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _response(content: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(content=content)


class _StubClient:
    """A canned Anthropic client: `messages.create` returns one response.

    Records the kwargs of the single create call so a test can assert the
    candidate enum was pinned off the live registry and `tool_choice` forced.
    """

    def __init__(self, response: SimpleNamespace) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)
        self._response = response

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._response


def _decomposed(items: list[dict[str, str]]) -> _StubClient:
    return _StubClient(_response([_tool_use_block("decompose_goal", {"sub_goals": items})]))


def _live_registry(tmp_path: Path) -> SubagentRegistry:
    """The real builtin registry (dlt/dbt/pipeline engineers + their labels)."""
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True)
    return AgentDiscovery.for_project(agents_dir=agents_dir).build_registry()


# --------------------------------------------------------------- multi-step happy


def test_multi_step_goal_decomposes_to_ordered_sub_goals(tmp_path: Path) -> None:
    """A two-step goal → an ORDERED list with the right per-step classifications."""
    registry = _live_registry(tmp_path)
    client = _decomposed(
        [
            {"sub_goal": "ingest the Stripe API", "classification": "new_pipeline"},
            {"sub_goal": "stage it with dbt", "classification": "new_model"},
        ]
    )

    result = decompose_goal(
        "ingest the Stripe API, then stage it with dbt",
        client=client,
        model="claude-opus-4-8",
        registry=registry,
    )

    assert result == [
        SubGoal(sub_goal="ingest the Stripe API", classification="new_pipeline"),
        SubGoal(sub_goal="stage it with dbt", classification="new_model"),
    ]
    # Order is preserved (dlt before dbt — work happens in that order).
    assert [s.classification for s in result] == ["new_pipeline", "new_model"]


def test_decompose_enum_is_live_candidate_set_and_tool_choice_pinned(tmp_path: Path) -> None:
    """The per-item enum is exactly the live candidate set; the tool is forced."""
    registry = _live_registry(tmp_path)
    client = _decomposed([{"sub_goal": "ingest the Stripe API", "classification": "new_pipeline"}])

    decompose_goal("ingest the Stripe API", client=client, model="m", registry=registry)

    sent_tools = client.calls[0]["tools"]
    item_schema = sent_tools[0]["input_schema"]["properties"]["sub_goals"]["items"]
    assert item_schema["properties"]["classification"]["enum"] == (
        candidate_classifications(registry)
    )
    # Forced to call the decompose tool (constrained, not an agentic loop).
    assert client.calls[0]["tool_choice"] == {"type": "tool", "name": "decompose_goal"}


def test_single_step_goal_decomposes_to_one_element(tmp_path: Path) -> None:
    """A single-step goal → a 1-element decomposition (the #44 N=1 case)."""
    registry = _live_registry(tmp_path)
    client = _decomposed([{"sub_goal": "ingest the Stripe API", "classification": "new_pipeline"}])

    result = decompose_goal(
        "ingest the Stripe API into the warehouse",
        client=client,
        model="m",
        registry=registry,
    )

    assert result == [SubGoal(sub_goal="ingest the Stripe API", classification="new_pipeline")]


# --------------------------------------------------------------- error paths


def test_out_of_set_classification_raises(tmp_path: Path) -> None:
    """A classification the model invents outside the candidate set is rejected."""
    registry = _live_registry(tmp_path)
    client = _decomposed(
        [
            {"sub_goal": "ingest the Stripe API", "classification": "new_pipeline"},
            {"sub_goal": "teleport the data", "classification": "teleport_data"},
        ]
    )

    with pytest.raises(GoalDecompositionError, match="not a registered classification"):
        decompose_goal("do a thing then another", client=client, model="m", registry=registry)


def test_no_tool_call_raises(tmp_path: Path) -> None:
    """A model that answers in prose (no tool call) yields a clear failure."""
    registry = _live_registry(tmp_path)
    client = _StubClient(_response([_text_block("This is two steps, I think.")]))

    with pytest.raises(GoalDecompositionError, match="no sub-goals"):
        decompose_goal("ingest then stage", client=client, model="m", registry=registry)


def test_empty_list_raises(tmp_path: Path) -> None:
    """An empty `sub_goals` array is treated as no decomposition."""
    registry = _live_registry(tmp_path)
    client = _decomposed([])

    with pytest.raises(GoalDecompositionError, match="no sub-goals"):
        decompose_goal("ingest then stage", client=client, model="m", registry=registry)


def test_empty_sub_goal_text_raises(tmp_path: Path) -> None:
    """An item with an empty `sub_goal` string is rejected (no partial route)."""
    registry = _live_registry(tmp_path)
    client = _decomposed([{"sub_goal": "   ", "classification": "new_pipeline"}])

    with pytest.raises(GoalDecompositionError, match="no sub-goals"):
        decompose_goal("ingest something", client=client, model="m", registry=registry)


def test_empty_goal_raises_without_calling_model(tmp_path: Path) -> None:
    """An empty goal never reaches the model."""
    registry = _live_registry(tmp_path)
    client = _decomposed([{"sub_goal": "ingest the Stripe API", "classification": "new_pipeline"}])

    with pytest.raises(GoalDecompositionError, match="empty goal"):
        decompose_goal("   ", client=client, model="m", registry=registry)
    assert client.calls == []


def test_no_classifications_in_registry_raises() -> None:
    """A registry whose agents declare no classifications has nothing to route on."""
    registry = SubagentRegistry()
    registry.register(
        AgentSpec(
            name="bare",
            system_prompt="x",
            capability=PermissionMode.BUILD,
            tool_factory=lambda _paths: [],
        )
    )
    client = _decomposed([{"sub_goal": "ingest the Stripe API", "classification": "new_pipeline"}])

    with pytest.raises(GoalDecompositionError, match="nothing to route on"):
        decompose_goal("g", client=client, model="m", registry=registry)
    assert client.calls == []
