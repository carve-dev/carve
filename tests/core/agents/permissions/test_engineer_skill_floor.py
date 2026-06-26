"""The engineer skill-tool permission floor — the regression net for the bug.

The dlt/dbt/pipeline engineers are granted domain skill tools
(``existing_dlt_inspect`` / ``dbt_manifest`` / ``dlt_library`` / …) bound via
``extra_tools``. Those names are NOT base harness tools, so before the floor fix
``build_policy`` intersected them out of ``permitted ∩ grant`` and the gate
denied them in *every* mode — a delegated engineer could never run its own
granted skills.

This module pins the contract that would have made that bug impossible to ship:
for each built-in engineer, every read-only skill in its ``tools:`` grant is
``permitted`` (gate ``allow``) at PLAN, READ_ONLY, and BUILD. It reads the grants
straight off the shipped ``builtin/*.md`` so a future engineer that grants a new
read skill must also floor it (or this test fails).
"""

from __future__ import annotations

import pytest

from carve.core.agents.discovery import BUILTIN_AGENTS_DIR
from carve.core.agents.loader import load_agent_file
from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import (
    _ALL_TOOLS,
    _SKILL_READ_TOOLS,
    AgentPolicy,
    build_policy,
)

# The three built-in engineers whose grants include extra_tools skill names. The
# read-only subset of each grant must be permitted at every non-write mode.
_ENGINEERS = ("dlt-engineer", "dbt-engineer", "pipeline-engineer")

# Modes at and below BUILD — the read floor must admit every read skill at all of
# them (a PLAN/READ_ONLY child inspects; a BUILD child inspects + writes).
_FLOOR_MODES = (PermissionMode.READ_ONLY, PermissionMode.PLAN, PermissionMode.BUILD)


def _grant(agent_name: str) -> frozenset[str]:
    """The shipped built-in engineer's ``tools:`` grant, read off its .md."""
    agent = load_agent_file(BUILTIN_AGENTS_DIR / f"{agent_name}.md")
    return frozenset(agent.tools)


def _read_skills_in_grant(grant: frozenset[str]) -> frozenset[str]:
    """The read-only engineer skill names this grant asks for."""
    return grant & _SKILL_READ_TOOLS


def _cases() -> list[tuple[str, str, PermissionMode]]:
    """(engineer, read_skill, mode) for every engineer x granted read skill x mode."""
    out: list[tuple[str, str, PermissionMode]] = []
    for engineer in _ENGINEERS:
        for skill in sorted(_read_skills_in_grant(_grant(engineer))):
            for mode in _FLOOR_MODES:
                out.append((engineer, skill, mode))
    return out


@pytest.mark.parametrize(("engineer", "skill", "mode"), _cases())
def test_engineer_read_skill_permitted_at_every_floor_mode(
    engineer: str, skill: str, mode: PermissionMode
) -> None:
    """Each engineer's granted read skill is permitted (gate allows) at the mode.

    This is the contract that pins the floor: ``permitted ∩ grant`` must retain
    the skill name, and the gate must ``allow`` a call to it. If the skill ever
    falls out of the read floor again, this fails for every (engineer, mode).
    """
    grant = _grant(engineer)
    policy = build_policy(
        mode,
        agent=AgentPolicy(tools=grant, capability=PermissionMode.BUILD),
    )
    # The intersected permitted set retains the granted read skill.
    assert policy.tool_permitted(skill), (
        f"{skill!r} dropped from {engineer}'s permitted set at {mode}"
    )
    # The gate (the live boundary the loop consults) allows the call.
    gate = PermissionGate(policy)
    decision = gate.check(skill, {"op": "list"})
    assert decision.allowed, f"gate denied {skill!r} for {engineer} at {mode}: {decision.reason}"


def test_each_engineer_grants_at_least_one_read_skill() -> None:
    """Guard the parametrization: every engineer actually exercises the floor.

    If a refactor renamed a grant or the skill set drifted, the case list could
    silently empty out and the parametrized test would pass vacuously. Pin that
    each built-in engineer grants ≥1 floored read skill so the floor stays under
    test.
    """
    for engineer in _ENGINEERS:
        read_skills = _read_skills_in_grant(_grant(engineer))
        assert read_skills, f"{engineer} grants no floored read skill — floor untested"


def test_every_granted_tool_is_known_to_the_policy() -> None:
    """Every tool a built-in engineer grants is a name the policy KNOWS.

    The bug class: a skill name granted in an engineer's ``tools:`` but absent
    from ``_SKILL_READ_TOOLS`` — so it is in neither ``_READ_TOOLS`` nor
    ``_ALL_TOOLS`` and the gate denies it in EVERY mode (``permitted ∩ grant``
    drops it). The old version of this test computed "skill grants" as
    ``grant & _SKILL_READ_TOOLS``, which pre-intersects the floor and so can
    NEVER see the missing name — it was tautological and let ``dbt_conventions``
    ship un-floored.

    The robust contract: every granted name (minus the ``mcp:*`` wildcard, which
    is resolved dynamically and is not a static policy tool) must be in
    ``_ALL_TOOLS`` (the full known taxonomy = base tools plus ``_SKILL_READ_TOOLS``).
    A granted skill forgotten in the floor is in neither and fails here.
    """
    for engineer in _ENGINEERS:
        granted = {t for t in _grant(engineer) if not t.startswith("mcp:")}
        unknown = granted - _ALL_TOOLS
        assert not unknown, (
            f"{engineer} grants tool(s) the policy doesn't know: {sorted(unknown)} "
            f"— a skill granted but absent from _SKILL_READ_TOOLS is gate-denied "
            f"at every mode (the #44 floor bug)."
        )
