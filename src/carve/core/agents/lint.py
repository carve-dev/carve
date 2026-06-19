"""The ``max_mode`` advisory lint — a warning, never a boundary.

An agent file declares ``tools:`` (the grant) and ``max_mode:`` (the
widest mode it ever needs). Some tools are *only ever* reachable at a
minimum mode tier — a write tool (``edit`` / ``create_file`` /
``write_file``) needs ``build``; warehouse DDL (``run_snowflake_ddl``)
needs ``deploy``. Granting such a tool while capping ``max_mode`` below
that tier means the tool can **never** fire: at the agent's widest mode
the runtime gate still attenuates it away.

This lint surfaces that misconfiguration as a ``logging.warning`` so an
author notices the dead grant. It is **explicitly NOT a security
boundary**:

* it does **not** raise,
* it does **not** drop the tool from the grant,
* it does **not** block the load.

The runtime gate (``policy.build_policy`` → ``runtime = grant ∩ mode``)
stays the sole authority. Per the spec's design note, grants are runtime
attenuation; ``max_mode`` is a helpful hint, not the control.
"""

from __future__ import annotations

import logging

from carve.core.agents.loader import AgentFile
from carve.core.agents.permissions.modes import (
    PermissionMode,
    mode_permits,
)
from carve.core.agents.permissions.policy import WRITE_TOOLS

logger = logging.getLogger(__name__)

# The minimum mode each privilege-tier tool needs to ever run. Derived
# from the gate's own clamps (``policy._permitted_tools_for_mode`` +
# ``gate.PermissionGate.check``): the write tools appear at ``build``;
# warehouse DDL only at ``deploy``. Tools absent from this map are
# reachable at any tier (``bash`` is gated per-command, not by mode) and
# never lint.
_TOOL_MIN_MODE: dict[str, PermissionMode] = {
    **{tool: PermissionMode.BUILD for tool in WRITE_TOOLS},
    # run_snowflake_ddl is in WRITE_TOOLS but needs deploy, not just build —
    # override it to the stricter tier so the lint is accurate.
    "run_snowflake_ddl": PermissionMode.DEPLOY,
}


def lint_agent_grants(agent: AgentFile) -> list[str]:
    """Warn (and return the messages) for unreachable-tool grants.

    For each granted tool whose minimum mode exceeds the agent's
    ``max_mode``, emit a ``logging.warning`` and collect the message. The
    return value is purely informational (the CLI / tests inspect it);
    the agent is **not** altered and the load is **not** blocked.
    """
    messages: list[str] = []
    for tool in agent.tools:
        min_mode = _TOOL_MIN_MODE.get(tool)
        if min_mode is None:
            continue
        if not mode_permits(agent.max_mode, min_mode):
            message = (
                f"Agent {agent.name!r} ({agent.source_path}) grants tool "
                f"{tool!r}, which needs at least {min_mode.value} mode, but "
                f"its max_mode is {agent.max_mode.value}; the runtime gate "
                "will always attenuate this tool away — the grant is dead. "
                "(This is an advisory lint, not a block.)"
            )
            logger.warning("%s", message)
            messages.append(message)
    return messages


__all__ = ["lint_agent_grants"]
