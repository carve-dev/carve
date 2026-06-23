"""Unit tests for the built-in ``dlt-qa`` + ``dlt-security`` reviewer agents.

The two shipped reviewer files at
``src/carve/core/agents/builtin/dlt-{qa,security}.md``:

* parse via the spec-16 safe loader (frontmatter + body),
* lint clean at ``read_only`` mode (no dead-grant findings — every tool they
  grant is reachable at ``read_only``),
* resolve a ``max_mode`` of ``READ_ONLY`` (they REPORT; they never edit),
* are discoverable by the loader/registry over the real built-in root.

These are read-only reviewers delegated by name (not classification-routed), so
discoverability is asserted via registry membership rather than ``select_agent``.
"""

from __future__ import annotations

import logging

import pytest

from carve.core.agents.discovery import BUILTIN_AGENTS_DIR, AgentDiscovery
from carve.core.agents.lint import lint_agent_grants
from carve.core.agents.loader import load_agent_file
from carve.core.agents.permissions.modes import PermissionMode

_QA_FILE = BUILTIN_AGENTS_DIR / "dlt-qa.md"
_SECURITY_FILE = BUILTIN_AGENTS_DIR / "dlt-security.md"

# (file, name, expected tools) for the two reviewers.
_REVIEWERS = (
    (_QA_FILE, "dlt-qa", ("grep", "glob", "sql", "read_file")),
    (_SECURITY_FILE, "dlt-security", ("grep", "glob", "read_file")),
)


def test_reviewer_files_exist_in_builtin_root() -> None:
    assert _QA_FILE.is_file(), f"missing built-in agent at {_QA_FILE}"
    assert _SECURITY_FILE.is_file(), f"missing built-in agent at {_SECURITY_FILE}"


@pytest.mark.parametrize(("path", "name", "tools"), _REVIEWERS)
def test_reviewer_parses_frontmatter_and_body(
    path: object, name: str, tools: tuple[str, ...]
) -> None:
    agent = load_agent_file(path)  # type: ignore[arg-type]
    assert agent.name == name
    # Read-only reviewers: they report, they never edit.
    assert agent.max_mode is PermissionMode.READ_ONLY
    assert agent.allowed_paths == ()
    # Falls back to default_model (no `model:` field), like dlt-engineer.
    assert agent.model is None
    assert agent.tools == tools
    # The body carries the role + the structured-findings output contract.
    assert "findings" in agent.body.lower()
    assert "reviewer" in agent.body.lower()
    # The reviewers run on a fresh, context-isolated read (diff + goal, not the
    # engineer's transcript) — stated in the body.
    assert "context-isolated" in agent.body.lower()


def test_security_reviewer_has_no_sql_grant() -> None:
    """dlt-security reviews from the diff alone — no warehouse access."""
    agent = load_agent_file(_SECURITY_FILE)
    assert "sql" not in agent.tools


def test_qa_reviewer_has_sql_grant() -> None:
    """dlt-qa introspects the real destination schema via the read-only sql tool."""
    agent = load_agent_file(_QA_FILE)
    assert "sql" in agent.tools


@pytest.mark.parametrize(("path", "name", "tools"), _REVIEWERS)
def test_grant_lints_clean_at_read_only(
    path: object,
    name: str,
    tools: tuple[str, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No dead-grant findings: ``grep``/``glob``/``read_file``/``sql`` are all
    reachable at ``read_only`` (absent from the lint's ``_TOOL_MIN_MODE``)."""
    agent = load_agent_file(path)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="carve.core.agents.lint"):
        messages = lint_agent_grants(agent)
    assert messages == [], messages
    assert not any(rec.levelno == logging.WARNING for rec in caplog.records)


@pytest.mark.parametrize(("path", "name", "tools"), _REVIEWERS)
def test_reviewer_discoverable_by_registry(
    path: object, name: str, tools: tuple[str, ...]
) -> None:
    """Both reviewers are discovered + registered from the real built-in root."""
    discovery = AgentDiscovery.for_project(
        agents_dir=BUILTIN_AGENTS_DIR.parent / "_no_user_agents_dir",
        builtin_dir=BUILTIN_AGENTS_DIR,
    )
    registry = discovery.build_registry()
    assert name in registry
    assert name in registry.names()
    spec = registry.resolve(name)
    assert spec.capability is PermissionMode.READ_ONLY
