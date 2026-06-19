"""Classification-routing tests (the AGENT_REGISTRY dispatch replacement)."""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.discovery import AgentDiscovery
from carve.core.agents.routing import NoAgentMatch, select_agent
from carve.core.agents.subagent_registry import SubagentRegistry

_DLT = """\
---
name: dlt-engineer
description: Ingest specialist.
max_mode: build
classifications: [new_pipeline, modify_pipeline]
---
prompt
"""

_DBT = """\
---
name: dbt-engineer
description: Transform specialist.
max_mode: build
classifications: [new_model, refactor_model]
---
prompt
"""


def _registry(tmp_path: Path) -> SubagentRegistry:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    builtin.mkdir(parents=True, exist_ok=True)
    user.mkdir(parents=True, exist_ok=True)
    (user / "dlt-engineer.md").write_text(_DLT, encoding="utf-8")
    (user / "dbt-engineer.md").write_text(_DBT, encoding="utf-8")
    return AgentDiscovery.for_project(
        agents_dir=user, builtin_dir=builtin
    ).build_registry()


def test_classification_resolves_to_matching_agent_name(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert select_agent(registry, classification="new_pipeline") == "dlt-engineer"
    assert select_agent(registry, classification="refactor_model") == "dbt-engineer"


def test_explicit_name_override_short_circuits(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    # Even with a dlt classification, an explicit dbt name wins.
    assert (
        select_agent(
            registry, classification="new_pipeline", override="dbt-engineer"
        )
        == "dbt-engineer"
    )


def test_override_naming_unknown_agent_raises(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(NoAgentMatch):
        select_agent(registry, override="ghost-agent")


def test_unmatched_classification_is_a_clear_no_match(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(NoAgentMatch):
        select_agent(registry, classification="not_a_real_classification")


def test_router_sees_user_override(tmp_path: Path) -> None:
    """A user file overriding a built-in participates in routing."""
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    builtin.mkdir(parents=True, exist_ok=True)
    user.mkdir(parents=True, exist_ok=True)
    # Built-in claims new_pipeline; user override moves it to a new class.
    (builtin / "dlt-engineer.md").write_text(_DLT, encoding="utf-8")
    override = _DLT.replace(
        "classifications: [new_pipeline, modify_pipeline]",
        "classifications: [incremental_pipeline]",
    )
    (user / "dlt-engineer.md").write_text(override, encoding="utf-8")

    registry = AgentDiscovery.for_project(
        agents_dir=user, builtin_dir=builtin
    ).build_registry()

    # The override's classification routes; the built-in's no longer does.
    assert (
        select_agent(registry, classification="incremental_pipeline")
        == "dlt-engineer"
    )
    with pytest.raises(NoAgentMatch):
        select_agent(registry, classification="new_pipeline")
