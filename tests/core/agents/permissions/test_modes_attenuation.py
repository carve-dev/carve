"""Unit tests for permission modes + grant attenuation at the gate.

The spec bar: ``read_only`` blocks ``edit``/warehouse-writes; an agent
granting ``bash`` runs with ``bash ∩ mode`` (a no-op set of write
subcommands in ``read_only``); a ``prompt``-tier action under a
non-interactive invocation resolves to DENY + ``needs_user_input``.
"""

from __future__ import annotations

from carve.core.agents.permissions.gate import Outcome, PermissionGate
from carve.core.agents.permissions.modes import (
    PermissionMode,
    min_mode,
    mode_for_verb,
)
from carve.core.agents.permissions.policy import AgentPolicy, build_policy


class TestLattice:
    def test_verb_to_mode(self) -> None:
        assert mode_for_verb("ask") is PermissionMode.READ_ONLY
        assert mode_for_verb("plan") is PermissionMode.PLAN
        assert mode_for_verb("build") is PermissionMode.BUILD
        assert mode_for_verb("deploy") is PermissionMode.DEPLOY

    def test_min_mode_picks_narrower(self) -> None:
        assert min_mode(PermissionMode.READ_ONLY, PermissionMode.BUILD) is (
            PermissionMode.READ_ONLY
        )
        assert min_mode(PermissionMode.DEPLOY, PermissionMode.BUILD) is (
            PermissionMode.BUILD
        )


class TestReadOnlyBlocksWrites:
    def test_edit_denied_in_read_only(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.READ_ONLY))
        decision = gate.check("edit", {"path": "x.py", "old_string": "a", "new_string": "b"})
        assert decision.outcome is Outcome.DENY

    def test_create_file_denied_in_plan(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.PLAN))
        decision = gate.check("create_file", {"path": "x.py", "content": "y"})
        assert decision.outcome is Outcome.DENY

    def test_warehouse_ddl_denied_below_deploy(self) -> None:
        for mode in (PermissionMode.READ_ONLY, PermissionMode.PLAN, PermissionMode.BUILD):
            gate = PermissionGate(build_policy(mode))
            decision = gate.check("run_snowflake_ddl", {"sql": "CREATE TABLE t (x int)"})
            assert decision.outcome is Outcome.DENY, mode

    def test_edit_allowed_in_build(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.BUILD))
        decision = gate.check(
            "edit", {"path": "x.py", "old_string": "a", "new_string": "b"}
        )
        assert decision.outcome is Outcome.ALLOW

    def test_warehouse_ddl_allowed_at_deploy(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.DEPLOY))
        decision = gate.check("run_snowflake_ddl", {"sql": "CREATE TABLE t (x int)"})
        assert decision.outcome is Outcome.ALLOW


class TestGrantAttenuation:
    def test_grant_cannot_widen_write_in_read_only(self) -> None:
        # An agent grants edit + bash, but read_only withholds edit
        # regardless of grant: the intersection drops it.
        agent = AgentPolicy(
            tools=frozenset({"edit", "bash", "read_file"}),
            capability=PermissionMode.BUILD,
        )
        policy = build_policy(PermissionMode.READ_ONLY, agent=agent)
        assert not policy.tool_permitted("edit")
        gate = PermissionGate(policy)
        assert gate.check(
            "edit", {"path": "x", "old_string": "a", "new_string": "b"}
        ).outcome is Outcome.DENY

    def test_bash_grant_runs_intersected_with_mode(self) -> None:
        # bash IS granted and IS permitted in read_only, but only read
        # subcommands clear the bash gate — a write subcommand is denied.
        agent = AgentPolicy(
            tools=frozenset({"bash", "read_file"}),
            capability=PermissionMode.BUILD,
        )
        gate = PermissionGate(build_policy(PermissionMode.READ_ONLY, agent=agent))
        assert gate.check("bash", {"command": "git status"}).outcome is Outcome.ALLOW
        assert gate.check("bash", {"command": "git commit -m x"}).outcome is Outcome.DENY

    def test_tool_not_granted_is_denied(self) -> None:
        # The agent didn't grant grep; even though the mode permits it,
        # the grant intersection removes it.
        agent = AgentPolicy(
            tools=frozenset({"read_file"}),
            capability=PermissionMode.BUILD,
        )
        gate = PermissionGate(build_policy(PermissionMode.BUILD, agent=agent))
        assert gate.check("grep", {"pattern": "x"}).outcome is Outcome.DENY
        assert gate.check("read_file", {"path": "x"}).outcome is Outcome.ALLOW


class TestNonInteractiveFailClosed:
    def test_prompt_tier_without_approver_is_needs_user_input(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.DEPLOY))
        # git push is a prompt-tier action at deploy; no approver → held.
        decision = gate.check("bash", {"command": "git push origin main"})
        assert decision.outcome is Outcome.NEEDS_USER_INPUT
        assert "approver" in decision.reason or "approval" in decision.reason

    def test_prompt_tier_with_declining_approver_is_needs_user_input(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.DEPLOY))
        decision = gate.check(
            "bash",
            {"command": "git push origin main"},
            approver=lambda _n, _i: False,
        )
        assert decision.outcome is Outcome.NEEDS_USER_INPUT


class TestReadFloorTools:
    def test_lookup_skill_pack_permitted_in_read_only(self) -> None:
        # `lookup_skill_pack` is a read-only content-injection tool (it
        # reads inert SKILL.md, writes nothing), so it must be in the
        # read-only floor — permitted from `read_only` up. The orchestrator
        # constructs agents WITH this tool; absent from the floor, a gated
        # loop would DENY a permitted, read-only injection.
        policy = build_policy(PermissionMode.READ_ONLY)
        assert policy.tool_permitted("lookup_skill_pack")
        gate = PermissionGate(policy)
        assert (
            gate.check("lookup_skill_pack", {"pack_name": "x"}).outcome
            is Outcome.ALLOW
        )

    def test_lookup_skill_pack_permitted_in_every_mode(self) -> None:
        for mode in PermissionMode:
            policy = build_policy(mode)
            assert policy.tool_permitted("lookup_skill_pack"), mode


class TestConfigTightenOnly:
    def test_config_can_remove_a_tool(self) -> None:
        from carve.core.agents.permissions.policy import PermissionsConfig

        config = PermissionsConfig(denied_tools=frozenset({"web_fetch"}))
        policy = build_policy(PermissionMode.BUILD, config=config)
        assert not policy.tool_permitted("web_fetch")
        # And it cannot add one the floor withholds (no widen path exists).
        assert policy.tool_permitted("read_file")
