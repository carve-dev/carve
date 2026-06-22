"""The gated, mode-clamped, fail-closed hook runner.

A hook's ``run = "<command>"`` executes through the **same bash gate** the
agent uses (no bypass) and the **same sandbox** (``run_bash`` — scrubbed
env, cwd-pin, bounded timeout). The security stance, point by point:

* **Same gate, no bypass.** The command is tiered by
  :meth:`PermissionGate.check` against the run's :class:`EffectivePolicy`,
  so the metacharacter screen, the argv allowlist, and the
  ``DANGEROUS_BASH_FLAGS`` denials all apply. A hook command containing
  ``$()`` / ``;`` / ``|`` is denied exactly as an agent ``bash`` call
  would be.
* **Mode-clamped.** Because the gate is fed the run's mode-clamped policy,
  a hook in ``read_only`` can't reach the network/git (those tiers aren't
  in the read-only bash rules) — the clamp is inherited, not re-derived.
* **Fail-closed.** A denied command, a non-zero exit, a timeout, or any
  exception **blocks the action** by raising :class:`HookExecutionError`.
  The loop already treats a raising ``pre_tool``/``post_tool`` hook as an
  abort, so blocking is the default.
* **No recursion.** A ``pre_*`` hook runs the gated ``bash`` *directly*
  via :func:`run_bash` — it does **not** go back through the loop's
  ``pre_tool`` pipeline, so a ``pre_tool`` hook can't re-trigger
  ``pre_tool`` hooks. A re-entrancy flag enforces this even if a future
  caller nests runners.
"""

from __future__ import annotations

import logging
from pathlib import Path

from carve.core.agents.permissions.gate import Approver, Outcome, PermissionGate
from carve.core.agents.tools.bash_tool import run_bash
from carve.core.hooks.config import HookSpec

logger = logging.getLogger(__name__)

# Hooks get a short, bounded budget — they are policy/lint glue, not the
# main work. Well under the bash tool's own 600s ceiling.
_HOOK_TIMEOUT_SECONDS = 60


class HookExecutionError(Exception):
    """Raised when a hook blocks the action (denied / non-zero / error).

    The loop turns a raising hook into a tool-call abort, so raising is the
    fail-closed signal. The message explains why the action was blocked.
    """


class HookRunner:
    """Runs hook commands through the gate + sandbox, fail-closed.

    Construct one per run with the run's :class:`PermissionGate` (built
    from the mode-clamped :class:`EffectivePolicy`) and the project root.
    The ``_in_hook`` flag is the no-recursion guard: while a hook command
    is executing, the runner refuses to start another hook (a ``pre_*``
    hook cannot re-enter the ``pre_tool`` pipeline).
    """

    def __init__(
        self,
        *,
        gate: PermissionGate,
        project_dir: Path,
        approver: Approver | None = None,
        timeout_seconds: int = _HOOK_TIMEOUT_SECONDS,
    ) -> None:
        self._gate = gate
        self._project_dir = project_dir.resolve()
        self._approver = approver
        self._timeout = timeout_seconds
        self._in_hook = False

    def run(self, spec: HookSpec, *, command: str | None = None) -> None:
        """Execute one hook command, fail-closed.

        ``command`` defaults to ``spec.run`` but may be substituted by the
        caller (after expanding ``{placeholders}``). Raises
        :class:`HookExecutionError` if the command is denied by the gate,
        exits non-zero, times out, or the runner is already inside a hook
        (no recursion).
        """
        if self._in_hook:
            # No recursion: a pre_* hook does not re-enter the pre_tool
            # pipeline. Refuse rather than silently nest.
            raise HookExecutionError(
                "Refusing to run a hook from within a hook (no re-entry); "
                "a pre_* hook cannot trigger further pre_* hooks."
            )

        cmd = command if command is not None else spec.run
        if not cmd.strip():
            raise HookExecutionError("Hook command is empty.")

        # Same gate as the agent's bash: tier the command. A deny /
        # needs-approval is fail-closed — the action is blocked.
        decision = self._gate.check("bash", {"command": cmd}, approver=self._approver)
        if decision.outcome is Outcome.DENY:
            raise HookExecutionError(f"Hook command denied by the bash gate: {decision.reason}")
        if decision.outcome is Outcome.NEEDS_USER_INPUT:
            raise HookExecutionError(
                f"Hook command needs approval (held / non-interactive): {decision.reason}"
            )

        self._in_hook = True
        try:
            result = run_bash(cmd, cwd=self._project_dir, timeout=self._timeout)
        except Exception as exc:
            # Any execution error blocks the action (fail-closed).
            raise HookExecutionError(f"Hook command errored: {exc}") from exc
        finally:
            self._in_hook = False

        if result.timed_out:
            raise HookExecutionError(f"Hook command timed out after {self._timeout}s: {cmd!r}")
        if result.exit_code != 0:
            raise HookExecutionError(
                f"Hook command exited {result.exit_code} (non-zero blocks "
                f"the action): {cmd!r}\n{result.stdout}"
            )


__all__ = ["HookExecutionError", "HookRunner"]
