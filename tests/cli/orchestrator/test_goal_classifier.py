"""Unit tests for `cli.orchestrator.goal_classifier.classify_goal`.

The classifier is a one-shot LLM call: it builds the candidate label set from
the **live registry** (the union of every agent's `classifications`), forces a
single `classify_goal` tool call, and re-validates the returned label against
the candidate set. A stub client returning a canned single-label tool-use
response makes it deterministic and offline. An out-of-set / empty answer
raises `GoalClassificationError` rather than routing to a wrong engine.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from carve.cli.orchestrator.goal_classifier import (
    GoalClassificationError,
    candidate_classifications,
    classify_goal,
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
    candidate enum was pinned off the live registry.
    """

    def __init__(self, response: SimpleNamespace) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)
        self._response = response

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._response


def _classified(label: str) -> _StubClient:
    return _StubClient(_response([_tool_use_block("classify_goal", {"label": label})]))


def _live_registry(tmp_path: Path) -> SubagentRegistry:
    """The real builtin registry (dlt/dbt/pipeline engineers + their labels)."""
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True)
    return AgentDiscovery.for_project(agents_dir=agents_dir).build_registry()


# --------------------------------------------------------------- candidate set


def test_candidate_set_is_union_of_registry_classifications(tmp_path: Path) -> None:
    """The candidate labels are the sorted union across the live agents."""
    registry = _live_registry(tmp_path)
    candidates = candidate_classifications(registry)

    # A representative label from each engineer family is present.
    assert "new_pipeline" in candidates  # dlt-engineer
    assert "new_model" in candidates  # dbt-engineer
    assert "compose_pipeline" in candidates  # pipeline-engineer
    # Sorted + de-duplicated.
    assert candidates == sorted(set(candidates))


# --------------------------------------------------------------- happy classes


@pytest.mark.parametrize(
    ("goal", "label"),
    [
        ("ingest the Stripe API into the warehouse", "new_pipeline"),
        ("add a staging model for orders", "new_model"),
        ("compose the daily ELT pipeline", "compose_pipeline"),
    ],
)
def test_classifies_goal_to_registered_label(tmp_path: Path, goal: str, label: str) -> None:
    """A canned label (dlt / dbt / pipeline) round-trips through the classifier."""
    registry = _live_registry(tmp_path)
    client = _classified(label)

    result = classify_goal(goal, client=client, model="claude-opus-4-8", registry=registry)

    assert result == label
    # The enum handed to the model is exactly the live candidate set.
    sent_tools = client.calls[0]["tools"]
    assert sent_tools[0]["input_schema"]["properties"]["label"]["enum"] == (
        candidate_classifications(registry)
    )
    # It was forced to call the classify tool (constrained, not an agentic loop).
    assert client.calls[0]["tool_choice"] == {"type": "tool", "name": "classify_goal"}


# --------------------------------------------------------------- error paths


def test_out_of_set_label_raises(tmp_path: Path) -> None:
    """A label the model invents outside the candidate set is rejected."""
    registry = _live_registry(tmp_path)
    client = _classified("teleport_data")

    with pytest.raises(GoalClassificationError, match="not a registered classification"):
        classify_goal("do a thing", client=client, model="m", registry=registry)


def test_no_tool_call_raises(tmp_path: Path) -> None:
    """A model that answers in prose (no tool call) yields a clear no-match."""
    registry = _live_registry(tmp_path)
    client = _StubClient(_response([_text_block("I think this is a pipeline.")]))

    with pytest.raises(GoalClassificationError, match="no label"):
        classify_goal("ingest something", client=client, model="m", registry=registry)


def test_empty_label_raises(tmp_path: Path) -> None:
    """A tool call carrying an empty label is treated as no answer."""
    registry = _live_registry(tmp_path)
    client = _classified("   ")

    with pytest.raises(GoalClassificationError, match="no label"):
        classify_goal("ingest something", client=client, model="m", registry=registry)


def test_empty_goal_raises_without_calling_model(tmp_path: Path) -> None:
    """An empty goal never reaches the model."""
    registry = _live_registry(tmp_path)
    client = _classified("new_pipeline")

    with pytest.raises(GoalClassificationError, match="empty goal"):
        classify_goal("   ", client=client, model="m", registry=registry)
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
    client = _classified("new_pipeline")

    with pytest.raises(GoalClassificationError, match="nothing to route on"):
        classify_goal("g", client=client, model="m", registry=registry)
    assert client.calls == []
