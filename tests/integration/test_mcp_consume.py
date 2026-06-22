"""Integration: MCP consume → policy registration → gate (the security path).

Every assertion goes through ``load_mcp_config`` → ``mcp_tool_specs`` →
``build_policy(..., mcp_tools=...)`` → ``EffectivePolicy`` →
``PermissionGate.check``, so the *gate* is the surface under test (not a
bespoke check). The properties pinned (spec 16 must-add #1):

* a registered **read-only-effects** MCP tool is permitted from
  ``read_only`` up;
* a tool **omitting effects** is treated as ``writes=true`` — denied in
  ``read_only``/``plan`` and **prompted** (held / fail-closed
  non-interactive) in ``build``/``deploy``;
* an **unregistered** ``mcp:`` name denies in every mode (closed-world);
* namespaced MCP tools don't collide with the base namespace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.permissions.gate import Outcome, PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import McpToolSpec, build_policy
from carve.core.mcp.client import (
    McpImportError,
    import_server_tools,
    mcp_tool_specs,
)
from carve.core.mcp.config import load_mcp_config

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "servers.toml"

_READ_TOOL = "mcp:fixture:search_issues"  # effects = ["read"] → read-only
_WRITE_TOOL = "mcp:fixture:create_issue"  # no effects → fail-closed writer


def _specs() -> frozenset[McpToolSpec]:
    config = load_mcp_config(_FIXTURE)
    return mcp_tool_specs(config.server)


def _gate(mode: PermissionMode) -> PermissionGate:
    # No agent — the registered MCP tools apply per their classification.
    policy = build_policy(mode, mcp_tools=_specs())
    return PermissionGate(policy)


def test_tools_are_namespaced_and_effects_tagged() -> None:
    config = load_mcp_config(_FIXTURE)
    imported = import_server_tools(config.server[0])
    by_name = {t.name: t for t in imported}
    assert set(by_name) == {_READ_TOOL, _WRITE_TOOL}
    assert by_name[_READ_TOOL].effects == ("read",)
    assert by_name[_READ_TOOL].writes is False
    # Missing effects → fail-closed writer.
    assert by_name[_WRITE_TOOL].effects == ()
    assert by_name[_WRITE_TOOL].writes is True


def test_read_only_tool_permitted_from_read_only() -> None:
    decision = _gate(PermissionMode.READ_ONLY).check(_READ_TOOL, {})
    assert decision.outcome is Outcome.ALLOW


def test_missing_effects_tool_denied_in_read_only_and_plan() -> None:
    for mode in (PermissionMode.READ_ONLY, PermissionMode.PLAN):
        decision = _gate(mode).check(_WRITE_TOOL, {})
        assert decision.outcome is Outcome.DENY, mode


def test_missing_effects_tool_prompted_in_build_and_deploy() -> None:
    # Non-interactive (no approver) → held for approval (fail-closed),
    # never auto-allowed.
    for mode in (PermissionMode.BUILD, PermissionMode.DEPLOY):
        decision = _gate(mode).check(_WRITE_TOOL, {})
        assert decision.outcome is Outcome.NEEDS_USER_INPUT, mode


def test_missing_effects_tool_allowed_with_approver_at_build() -> None:
    decision = _gate(PermissionMode.BUILD).check(_WRITE_TOOL, {}, approver=lambda _n, _i: True)
    assert decision.outcome is Outcome.ALLOW


def test_unregistered_mcp_name_denies_in_every_mode() -> None:
    ghost = "mcp:fixture:not_registered"
    for mode in PermissionMode:
        decision = _gate(mode).check(ghost, {})
        assert decision.outcome is Outcome.DENY, mode


def test_mcp_names_do_not_collide_with_base_namespace() -> None:
    # The base tools (edit/read_file/bash/…) are unaffected: a registered
    # read-only MCP tool does not appear as a base tool, and an MCP
    # registration never widens a base write tool below build.
    policy = build_policy(PermissionMode.READ_ONLY, mcp_tools=_specs())
    assert _READ_TOOL in policy.permitted_tools
    # Base write tool stays denied below build regardless of MCP registration.
    assert "edit" not in policy.permitted_tools


def test_duplicate_tool_name_within_a_server_raises() -> None:
    """Two tools with the same name on one server → McpImportError.

    The ``mcp:<server>:<tool>`` name would collide, so the import refuses
    rather than silently dropping one.
    """
    from carve.core.mcp.config import McpServer, McpToolDecl

    server = McpServer(
        name="dup",
        command="dup-mcp --stdio",
        tools=[
            McpToolDecl(name="search", effects=["read"]),
            McpToolDecl(name="search", effects=["read"]),
        ],
    )
    with pytest.raises(McpImportError, match="twice"):
        import_server_tools(server)


def test_incomplete_effects_mixed_read_and_write_is_a_writer() -> None:
    """A read tag mixed with a write/unknown tag classifies writes=true.

    ``effects=('read','delete')`` is *incomplete* read-only proof — one tag
    is read-only, but ``delete`` is not in the read-only allowlist — so the
    fail-closed derivation treats the whole tool as a writer. Asserted
    through the real import → policy → gate path: denied in read_only/plan,
    prompted (held / fail-closed non-interactive) in build/deploy.
    """
    from carve.core.mcp.config import McpServer, McpToolDecl

    server = McpServer(
        name="mixed",
        command="mixed-mcp --stdio",
        tools=[McpToolDecl(name="purge", effects=["read", "delete"])],
    )
    imported = import_server_tools(server)
    assert imported[0].writes is True  # one non-read tag flips it to a writer

    specs = mcp_tool_specs([server])
    tool = "mcp:mixed:purge"

    for mode in (PermissionMode.READ_ONLY, PermissionMode.PLAN):
        policy = build_policy(mode, mcp_tools=specs)
        assert PermissionGate(policy).check(tool, {}).outcome is Outcome.DENY, mode

    for mode in (PermissionMode.BUILD, PermissionMode.DEPLOY):
        policy = build_policy(mode, mcp_tools=specs)
        # Non-interactive → held for approval (prompt tier), never auto-allow.
        decision = PermissionGate(policy).check(tool, {})
        assert decision.outcome is Outcome.NEEDS_USER_INPUT, mode


def test_agent_grant_narrows_mcp_tools() -> None:
    """An agent only gets the MCP tools it actually granted."""
    from carve.core.agents.permissions.policy import AgentPolicy

    # Agent grants the read tool but NOT the write tool.
    agent = AgentPolicy(
        tools=frozenset({_READ_TOOL, "read_file"}),
        capability=PermissionMode.DEPLOY,
    )
    policy = build_policy(PermissionMode.DEPLOY, agent=agent, mcp_tools=_specs())
    gate = PermissionGate(policy)
    assert gate.check(_READ_TOOL, {}).outcome is Outcome.ALLOW
    # Not granted → not permitted, even at deploy.
    assert gate.check(_WRITE_TOOL, {}).outcome is Outcome.DENY


def test_mcp_tool_spec_rejects_non_mcp_prefixed_name() -> None:
    """A crafted McpToolSpec(name='edit') can never be constructed.

    The ``mcp:`` prefix is a hard precondition of the type — enforced at
    the widening point, not merely belt-checked against WRITE_TOOLS — so a
    non-namespaced name can never enter ``permitted_tools``.
    """
    with pytest.raises(ValueError, match="must start with 'mcp:'"):
        McpToolSpec(name="edit", writes=False)
    with pytest.raises(ValueError, match="must start with 'mcp:'"):
        McpToolSpec(name="read_file", writes=False)
    # A legitimately-namespaced name is accepted.
    assert McpToolSpec(name="mcp:jira:search", writes=False).name == ("mcp:jira:search")


def test_wildcard_grant_admits_a_servers_imported_tools() -> None:
    """``tools: ["mcp:fixture:*"]`` admits that server's tools (∩ mode/effects)."""
    from carve.core.agents.permissions.policy import AgentPolicy

    # Grant the whole server via wildcard. At deploy the writer is admitted
    # (prompt-tier); the read tool is admitted outright.
    agent = AgentPolicy(
        tools=frozenset({"mcp:fixture:*", "read_file"}),
        capability=PermissionMode.DEPLOY,
    )
    policy = build_policy(PermissionMode.DEPLOY, agent=agent, mcp_tools=_specs())
    assert _READ_TOOL in policy.permitted_tools
    assert _WRITE_TOOL in policy.permitted_tools
    # The writer is still effects-classified — prompt-tier, not auto-allow.
    assert policy.mcp_requires_prompt(_WRITE_TOOL)

    gate = PermissionGate(policy)
    assert gate.check(_READ_TOOL, {}).outcome is Outcome.ALLOW
    assert gate.check(_WRITE_TOOL, {}, approver=lambda _n, _i: True).outcome is Outcome.ALLOW


def test_wildcard_grant_is_server_scoped() -> None:
    """``mcp:other:*`` does not admit a different server's tools."""
    from carve.core.agents.permissions.policy import AgentPolicy

    agent = AgentPolicy(
        tools=frozenset({"mcp:other:*", "read_file"}),
        capability=PermissionMode.DEPLOY,
    )
    policy = build_policy(PermissionMode.DEPLOY, agent=agent, mcp_tools=_specs())
    # The fixture server's read tool is NOT admitted by an other-server
    # wildcard — the wildcard is scoped to its own server prefix.
    assert _READ_TOOL not in policy.permitted_tools
    assert _WRITE_TOOL not in policy.permitted_tools
