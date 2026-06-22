"""The `sql` tool is admitted by the gate in every mode (write/DDL gated in-tool)."""

from __future__ import annotations

from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy


def test_sql_tool_permitted_in_every_mode() -> None:
    # The gate is name-only; `sql` must be reachable in every mode (its reads
    # always; its writes are fail-closed inside the tool via warehouse_roles).
    # If it weren't in the permitted set, the closed-world gate would deny it
    # even in deploy, exactly when writes need it.
    for mode in PermissionMode:
        assert build_policy(mode).tool_permitted("sql"), mode
