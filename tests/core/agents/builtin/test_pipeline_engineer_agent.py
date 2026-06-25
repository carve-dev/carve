"""Unit tests for the built-in ``pipeline-engineer`` declarative agent.

The shipped agent file at ``src/carve/core/agents/builtin/pipeline-engineer.md``:

* parses via the spec-16 safe loader (frontmatter + body),
* lints clean for ``build`` mode (no dead-grant findings — its one write grant,
  ``edit``, is reachable at ``build``; the skill tools ``pipeline_inspect`` /
  ``list_components`` / ``list_dbt_models`` / ``sql`` are absent from the lint's
  ``_TOOL_MIN_MODE`` so they never lint),
* is ``select_agent``-routable by each of its four classifications from a
  registry built over the real built-in discovery root,
* is overridden by a ``carve/agents/pipeline-engineer.md`` user file (smoke).
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

_AGENT_FILE = BUILTIN_AGENTS_DIR / "pipeline-engineer.md"

_CLASSIFICATIONS = (
    "compose_pipeline",
    "modify_pipeline_steps",
    "seed_schedule",
    "schedule_existing_component",
)


def test_agent_file_exists_in_builtin_root() -> None:
    assert _AGENT_FILE.is_file(), f"missing built-in agent at {_AGENT_FILE}"


def test_agent_parses_frontmatter_and_body() -> None:
    agent = load_agent_file(_AGENT_FILE)
    assert agent.name == "pipeline-engineer"
    assert agent.description.lower().startswith("composes existing dlt/dbt/sql")
    # max_mode = build; falls back to default_model (no `model:` field).
    assert agent.max_mode is PermissionMode.BUILD
    assert agent.model is None
    # The full tool grant from the spec: one write tool (`edit`), read/search
    # (`grep`), the three pipeline skills, `sql`, `web_fetch`, and the `mcp:*`
    # passthrough grant the dlt/dbt engineers also carry.
    assert agent.tools == (
        "edit",
        "grep",
        "pipeline_inspect",
        "list_components",
        "list_dbt_models",
        "sql",
        "web_fetch",
        "mcp:*",
    )
    # `edit` scoped to `pipelines/**` is the ONLY write scope — the engineer
    # never writes `el/**`, `carve/**`, or `[components.*]` blocks.
    assert agent.allowed_paths == ("pipelines/**",)
    assert agent.classifications == _CLASSIFICATIONS
    # The system-prompt body carries the role + the verify-by-validate loop.
    assert "pipeline engineer" in agent.body.lower()
    assert "carve pipelines validate" in agent.body
    # The verify-by-validate discipline (not free-form bash) is stated.
    assert "verify before returning" in agent.body.lower()
    # Composition is BY NAME; it never authors components or `[components.*]`.
    assert 'component = "<name>"' in agent.body
    assert "[components.*]" in agent.body
    # Schedule semantics: `[seed_schedule]` is a seed, live changes route to the
    # schedule CLI — not a TOML rewrite.
    assert "[seed_schedule]" in agent.body
    assert "carve schedule" in agent.body
    # Orchestration-only mode (mode 2): compose against an existing component.
    assert "schedule_existing_component" in agent.body


def test_grant_lints_clean_for_build(caplog: pytest.LogCaptureFixture) -> None:
    """No dead-grant findings: the one write tool (``edit``) is reachable at
    ``build``; the skill tools aren't in the lint's ``_TOOL_MIN_MODE``."""
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
    assert select_agent(registry, classification=classification) == "pipeline-engineer"


def test_user_override_wins(tmp_path: Path) -> None:
    """A ``carve/agents/pipeline-engineer.md`` user file shadows the built-in."""
    user_dir = tmp_path / "carve" / "agents"
    user_dir.mkdir(parents=True)
    override = (
        "---\n"
        "name: pipeline-engineer\n"
        "description: Overridden pipeline engineer.\n"
        "max_mode: build\n"
        "classifications: [overridden_class]\n"
        "---\n"
        "Overridden body.\n"
    )
    (user_dir / "pipeline-engineer.md").write_text(override, encoding="utf-8")

    registry = AgentDiscovery.for_project(
        agents_dir=user_dir,
        builtin_dir=BUILTIN_AGENTS_DIR,
    ).build_registry()

    # The override's classification routes; the built-in's no longer does.
    assert select_agent(registry, classification="overridden_class") == "pipeline-engineer"
    with pytest.raises(NoAgentMatch):
        select_agent(registry, classification="compose_pipeline")
