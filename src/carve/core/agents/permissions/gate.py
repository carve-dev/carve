"""The single pre-execution permission gate — the authoritative boundary.

Every tool call passes through :meth:`PermissionGate.check` *before* the
loop dispatches it to the tool executor. The gate's verdict — `allow` /
`deny(reason)` / `needs_user_input` — is the airtight security surface
the harness rests on. It is intentionally the **only** boundary: grants
live in editable agent files and so can never be trusted; the runtime
gate, fed an :class:`EffectivePolicy` whose permitted set is already the
``mode ∩ config ∩ agent`` intersection, is what actually stops a call.

Three rules the gate enforces that nothing upstream can override:

* **Write tools are denied below ``build``.** ``edit`` / ``create_file``
  / ``write_file`` / ``run_snowflake_ddl`` (and ``bash``-writes, via the
  bash gate's tiering) cannot run in ``read_only`` / ``plan`` *no matter
  what an agent granted* — the permitted set already excludes them, and
  this is a second, explicit check.
* **bash is tiered per-command** by :func:`tier_bash_command` (metachar
  deny + argv allowlist), not by a blanket allow.
* **Non-interactive == fail-closed.** A ``prompt``-tier outcome with no
  registered interactive ``approver`` resolves to **deny +
  needs_user_input**, never auto-allow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from carve.core.agents.permissions.bash_gate import tier_bash_command
from carve.core.agents.permissions.modes import PermissionMode, mode_permits
from carve.core.agents.permissions.policy import WRITE_TOOLS, EffectivePolicy

# An approver callback is handed the tool name + input and returns True to
# allow a `prompt`-tier action. Absent (``None``) means non-interactive →
# every prompt is denied. Registering one is how `carve` chat (with a TTY)
# lets a human approve a push; `carve serve`/REST/CI pass ``None``.
Approver = Callable[[str, dict[str, Any]], bool]


class Outcome(StrEnum):
    """The three gate verdicts."""

    ALLOW = "allow"
    DENY = "deny"
    NEEDS_USER_INPUT = "needs_user_input"


@dataclass(frozen=True)
class Decision:
    """A gate verdict plus a reason for the non-allow cases.

    The loop turns a ``DENY`` into an ``is_error=True`` tool_result, and a
    ``NEEDS_USER_INPUT`` into a surfaced "needs approval" outcome — in
    both cases the tool executor is **not** called.
    """

    outcome: Outcome
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    @staticmethod
    def allow() -> Decision:
        return Decision(Outcome.ALLOW)

    @staticmethod
    def deny(reason: str) -> Decision:
        return Decision(Outcome.DENY, reason)

    @staticmethod
    def needs_user_input(reason: str) -> Decision:
        return Decision(Outcome.NEEDS_USER_INPUT, reason)


# Tools that only capture a terminator payload — never gated (they touch
# nothing). Resolved by suffix/name so per-agent terminators
# (`submit_result`, `submit_plan`, `submit_step`, `submit_diagnosis`) all
# pass without needing to be in every mode's permitted set.
_TERMINATOR_TOOLS: frozenset[str] = frozenset(
    {"submit_result", "submit_plan", "submit_step", "submit_diagnosis"}
)


class PermissionGate:
    """Stateless policy enforcer the loop calls once per tool use.

    Construct one per run with the run's :class:`EffectivePolicy`; pass an
    ``approver`` only for interactive sessions. ``check`` is pure given
    the policy + input.
    """

    def __init__(self, policy: EffectivePolicy) -> None:
        self._policy = policy

    def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        approver: Approver | None = None,
    ) -> Decision:
        """Decide whether ``tool_name(tool_input)`` may run.

        Order:
        1. Terminator tools pass unconditionally (they only capture).
        2. The tool must be in the intersected permitted set, else deny.
        3. Write tools are denied below ``build`` (explicit, belt-and-
           braces over the permitted-set intersection).
        4. ``bash`` is tiered per-command; a ``prompt`` tier needs an
           approver (else deny + needs_user_input).
        """
        if tool_name in _TERMINATOR_TOOLS:
            return Decision.allow()

        if not self._policy.tool_permitted(tool_name):
            return Decision.deny(
                f"Tool {tool_name!r} is not permitted in {self._policy.mode} mode."
            )

        # Explicit write-tier clamp. The permitted set already excludes
        # these below `build`, but we re-assert here so the invariant is
        # legible at the gate and survives any future widening of the set.
        if tool_name in WRITE_TOOLS and not mode_permits(
            self._policy.mode, PermissionMode.BUILD
        ):
            return Decision.deny(
                f"Write tool {tool_name!r} is denied in {self._policy.mode} mode; "
                "writes require build or deploy."
            )

        # `run_snowflake_ddl` is a warehouse write: deploy-tier only.
        if tool_name == "run_snowflake_ddl" and not mode_permits(
            self._policy.mode, PermissionMode.DEPLOY
        ):
            return Decision.deny(
                f"Warehouse DDL is denied in {self._policy.mode} mode; "
                "it requires deploy."
            )

        if tool_name == "bash":
            return self._check_bash(tool_input, approver=approver)

        return Decision.allow()

    def _check_bash(
        self,
        tool_input: dict[str, Any],
        *,
        approver: Approver | None,
    ) -> Decision:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return Decision.deny("`command` must be a string.")

        decision = tier_bash_command(command, self._policy.bash)
        if decision.tier == "allow":
            return Decision.allow()
        if decision.tier == "deny":
            return Decision.deny(decision.reason)

        # prompt tier — fail closed unless an interactive approver clears it.
        if approver is None:
            return Decision.needs_user_input(
                f"{decision.reason} No interactive approver is registered "
                "(non-interactive run), so this is held for user approval."
            )
        if approver("bash", dict(tool_input)):
            return Decision.allow()
        return Decision.needs_user_input(
            f"{decision.reason} The approver declined; held for user input."
        )


def is_write_path_allowed(
    candidate: Path,
    *,
    project_root: Path,
    allowed_paths: frozenset[Path] | None,
) -> bool:
    """Return True iff a resolved write ``candidate`` is in scope.

    This is the path clamp the write tools (`fs_tools`) consult in
    addition to the gate's tool-level check: a write must land under the
    project root, and — when ``allowed_paths`` is set — be one of those
    exact resolved paths. ``allowed_paths is None`` means "project-root
    containment only" (the tool was built without a narrower list).
    """
    candidate = candidate.resolve()
    root = project_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    if allowed_paths is None:
        return True
    return candidate in {p.resolve() for p in allowed_paths}


__all__ = [
    "Approver",
    "Decision",
    "Outcome",
    "PermissionGate",
    "is_write_path_allowed",
]
