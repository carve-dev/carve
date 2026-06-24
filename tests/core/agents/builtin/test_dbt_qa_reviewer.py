"""Unit tests for the built-in ``dbt-qa`` reviewer agent.

The shipped reviewer file at ``src/carve/core/agents/builtin/dbt-qa.md``:

* parses via the spec-16 safe loader (frontmatter + body),
* lints clean at ``read_only`` mode (no dead-grant findings — every tool it
  grants is reachable at ``read_only``),
* resolves a ``max_mode`` of ``READ_ONLY`` (it REPORTS; it never edits),
* is discoverable by the loader/registry over the real built-in root.

This is a read-only reviewer delegated by name (not classification-routed), so
discoverability is asserted via registry membership rather than ``select_agent``.
"""

from __future__ import annotations

import logging

import pytest

from carve.core.agents.discovery import BUILTIN_AGENTS_DIR, AgentDiscovery
from carve.core.agents.lint import lint_agent_grants
from carve.core.agents.loader import load_agent_file
from carve.core.agents.permissions.modes import PermissionMode

_QA_FILE = BUILTIN_AGENTS_DIR / "dbt-qa.md"

# The dbt-qa reviewer reads the project graph (`dbt_manifest`) and the real
# warehouse schema (`sql`), unlike dlt-qa — coverage checks need the manifest.
_EXPECTED_TOOLS = ("grep", "glob", "sql", "read_file", "dbt_manifest")


def test_reviewer_file_exists_in_builtin_root() -> None:
    assert _QA_FILE.is_file(), f"missing built-in agent at {_QA_FILE}"


def test_reviewer_parses_frontmatter_and_body() -> None:
    agent = load_agent_file(_QA_FILE)
    assert agent.name == "dbt-qa"
    # Read-only reviewer: it reports, it never edits.
    assert agent.max_mode is PermissionMode.READ_ONLY
    assert agent.allowed_paths == ()
    # Falls back to default_model (no `model:` field), like dlt-qa.
    assert agent.model is None
    assert agent.tools == _EXPECTED_TOOLS
    # The body carries the role + the structured-findings output contract.
    assert "findings" in agent.body.lower()
    assert "reviewer" in agent.body.lower()
    # The reviewer runs on a fresh, context-isolated read (diff + goal, not the
    # engineer's transcript) — stated in the body.
    assert "context-isolated" in agent.body.lower()
    # The three review axes the spec demands.
    body_lower = agent.body.lower()
    assert "test coverage" in body_lower
    assert "convention" in body_lower
    assert "sql quality" in body_lower


def test_reviewer_has_sql_and_manifest_grants() -> None:
    """dbt-qa introspects the real schema (`sql`) and the project graph
    (`dbt_manifest`) — both read-only."""
    agent = load_agent_file(_QA_FILE)
    assert "sql" in agent.tools
    assert "dbt_manifest" in agent.tools


def test_grant_lints_clean_at_read_only(caplog: pytest.LogCaptureFixture) -> None:
    """No dead-grant findings: ``grep``/``glob``/``read_file``/``sql``/
    ``dbt_manifest`` are all reachable at ``read_only`` (absent from the lint's
    ``_TOOL_MIN_MODE``)."""
    agent = load_agent_file(_QA_FILE)
    with caplog.at_level(logging.WARNING, logger="carve.core.agents.lint"):
        messages = lint_agent_grants(agent)
    assert messages == [], messages
    assert not any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_reviewer_discoverable_by_registry() -> None:
    """The reviewer is discovered + registered from the real built-in root."""
    discovery = AgentDiscovery.for_project(
        agents_dir=BUILTIN_AGENTS_DIR.parent / "_no_user_agents_dir",
        builtin_dir=BUILTIN_AGENTS_DIR,
    )
    registry = discovery.build_registry()
    assert "dbt-qa" in registry
    assert "dbt-qa" in registry.names()
    spec = registry.resolve("dbt-qa")
    assert spec.capability is PermissionMode.READ_ONLY
