"""Unit tests for the built-in ``dlt-engineer`` declarative agent.

The shipped agent file at ``src/carve/core/agents/builtin/dlt-engineer.md``:

* parses via the spec-16 safe loader (frontmatter + body),
* lints clean for ``build`` mode (no dead-grant findings — every write grant
  is reachable at ``build``),
* is ``select_agent``-routable by each of its five classifications from a
  registry built over the real built-in discovery root,
* is overridden by a ``carve/agents/dlt-engineer.md`` user file (smoke).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from carve.core.agents.discovery import BUILTIN_AGENTS_DIR, AgentDiscovery
from carve.core.agents.lint import lint_agent_grants
from carve.core.agents.loader import load_agent_file
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.routing import NoAgentMatch, select_agent

_AGENT_FILE = BUILTIN_AGENTS_DIR / "dlt-engineer.md"

_CLASSIFICATIONS = (
    "new_pipeline",
    "modify_pipeline",
    "refactor_pipeline_to_incremental",
    "add_resource_to_pipeline",
    "update_pipeline_destination",
)


def test_agent_file_exists_in_builtin_root() -> None:
    assert _AGENT_FILE.is_file(), f"missing built-in agent at {_AGENT_FILE}"


def test_agent_parses_frontmatter_and_body() -> None:
    agent = load_agent_file(_AGENT_FILE)
    assert agent.name == "dlt-engineer"
    assert agent.description.lower().startswith("authors and runs dlt")
    # max_mode = build; falls back to default_model (no `model:` field).
    assert agent.max_mode is PermissionMode.BUILD
    assert agent.model is None
    # The full tool grant from the spec.
    assert agent.tools == (
        "edit",
        "create_file",
        "bash",
        "grep",
        "glob",
        "web_fetch",
        "sql",
        "dlt_library",
        "rest_api_explore",
        "dbt_source_lookup",
        "existing_dlt_inspect",
        "mcp:*",
    )
    assert agent.allowed_paths == ("el/**", ".dlt/*.template")
    assert agent.classifications == _CLASSIFICATIONS
    # The system-prompt body carries the role + the verification loop.
    assert "DLT engineer" in agent.body
    assert "verification loop" in agent.body.lower()


def test_grant_lints_clean_for_build(caplog: pytest.LogCaptureFixture) -> None:
    """No dead-grant findings: every write tool is reachable at ``build``."""
    agent = load_agent_file(_AGENT_FILE)
    with caplog.at_level(logging.WARNING, logger="carve.core.agents.lint"):
        messages = lint_agent_grants(agent)
    assert messages == [], messages
    assert not any(rec.levelno == logging.WARNING for rec in caplog.records)


@pytest.mark.parametrize("classification", _CLASSIFICATIONS)
def test_routable_by_each_classification(classification: str) -> None:
    """The agent routes for every classification it declares, from a registry
    built over the real built-in discovery root."""
    discovery = AgentDiscovery.for_project(
        agents_dir=BUILTIN_AGENTS_DIR.parent / "_no_user_agents_dir",
        builtin_dir=BUILTIN_AGENTS_DIR,
    )
    registry = discovery.build_registry()
    assert select_agent(registry, classification=classification) == "dlt-engineer"


def test_user_override_wins(tmp_path: Path) -> None:
    """A ``carve/agents/dlt-engineer.md`` user file shadows the built-in (smoke)."""
    user_dir = tmp_path / "carve" / "agents"
    user_dir.mkdir(parents=True)
    override = (
        "---\n"
        "name: dlt-engineer\n"
        "description: Overridden dlt engineer.\n"
        "max_mode: build\n"
        "classifications: [overridden_class]\n"
        "---\n"
        "Overridden body.\n"
    )
    (user_dir / "dlt-engineer.md").write_text(override, encoding="utf-8")

    registry = AgentDiscovery.for_project(
        agents_dir=user_dir,
        builtin_dir=BUILTIN_AGENTS_DIR,
    ).build_registry()

    # The override's classification routes; the built-in's no longer does.
    assert select_agent(registry, classification="overridden_class") == "dlt-engineer"
    with pytest.raises(NoAgentMatch):
        select_agent(registry, classification="new_pipeline")
