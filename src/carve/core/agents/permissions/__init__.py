"""The harness permission system — the authoritative pre-execution boundary.

Public surface:

* :class:`PermissionMode` + :func:`min_mode` / :func:`mode_for_verb` —
  the authority lattice and the verb→mode map (`modes`).
* :class:`AgentPolicy` / :class:`EffectivePolicy` / :func:`build_policy`
  / :class:`PermissionsConfig` — the hardcoded per-mode floor and the
  ``mode ∩ config ∩ agent`` (tighten-only) reconciliation (`policy`).
* :class:`PermissionGate` / :class:`Decision` / :class:`Outcome` /
  :class:`Approver` / :func:`is_write_path_allowed` — the single gate
  the loop calls before every tool dispatch (`gate`).
* :func:`tier_bash_command` / :class:`BashDecision` — the bash tiering
  surface (`bash_gate`).

Nothing here executes a tool; this package decides *whether* a tool may
run. The loop's ``_execute_tool_calls`` is where the gate is wired in.
"""

from __future__ import annotations

from carve.core.agents.permissions.bash_gate import BashDecision, tier_bash_command
from carve.core.agents.permissions.gate import (
    Approver,
    Decision,
    Outcome,
    PermissionGate,
    is_write_path_allowed,
)
from carve.core.agents.permissions.modes import (
    PermissionMode,
    min_mode,
    mode_for_verb,
    mode_permits,
    rank,
)
from carve.core.agents.permissions.policy import (
    WRITE_TOOLS,
    AgentPolicy,
    BashRules,
    EffectivePolicy,
    PermissionsConfig,
    build_policy,
)
from carve.core.agents.permissions.warehouse_roles import (
    WarehouseRole,
    WarehouseWriteDenied,
    role_for,
)

__all__ = [
    "WRITE_TOOLS",
    "AgentPolicy",
    "Approver",
    "BashDecision",
    "BashRules",
    "Decision",
    "EffectivePolicy",
    "Outcome",
    "PermissionGate",
    "PermissionMode",
    "PermissionsConfig",
    "WarehouseRole",
    "WarehouseWriteDenied",
    "build_policy",
    "is_write_path_allowed",
    "min_mode",
    "mode_for_verb",
    "mode_permits",
    "rank",
    "role_for",
    "tier_bash_command",
]
