"""Hook-runner tests: gated, mode-clamped, fail-closed, no-recursion."""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.hooks.config import HookMatch, HookSpec
from carve.core.hooks.events import HookEvent
from carve.core.hooks.runner import HookExecutionError, HookRunner
from carve.core.hooks.wiring import build_tool_hooks


def _gate(mode: PermissionMode = PermissionMode.BUILD) -> PermissionGate:
    return PermissionGate(build_policy(mode))


def _runner(tmp_path: Path, mode: PermissionMode = PermissionMode.BUILD) -> HookRunner:
    return HookRunner(gate=_gate(mode), project_dir=tmp_path)


def test_non_zero_exit_blocks_the_call(tmp_path: Path) -> None:
    # `false` is allow-listed in every mode and exits non-zero.
    spec = HookSpec(event=HookEvent.PRE_TOOL, run="false")
    with pytest.raises(HookExecutionError, match="non-zero"):
        _runner(tmp_path).run(spec)


def test_zero_exit_does_not_block(tmp_path: Path) -> None:
    spec = HookSpec(event=HookEvent.PRE_TOOL, run="true")
    # No raise == the action proceeds.
    _runner(tmp_path).run(spec)


def test_metacharacter_command_denied_by_bash_gate(tmp_path: Path) -> None:
    for cmd in ("echo hi; rm -rf /", "echo $(whoami)", "echo a && echo b"):
        spec = HookSpec(event=HookEvent.PRE_TOOL, run=cmd)
        with pytest.raises(HookExecutionError, match="denied by the bash gate"):
            _runner(tmp_path).run(spec)


def test_disallowed_program_denied_by_bash_gate(tmp_path: Path) -> None:
    # `curl` is always-denied (network egress) — same gate, no bypass.
    spec = HookSpec(event=HookEvent.PRE_TOOL, run="curl http://evil")
    with pytest.raises(HookExecutionError, match="denied by the bash gate"):
        _runner(tmp_path).run(spec)


def test_mode_clamp_denies_write_command_in_read_only(tmp_path: Path) -> None:
    # `git commit` is a write subcommand — allowed at build, denied below.
    spec = HookSpec(event=HookEvent.PRE_TOOL, run="git commit -m x")
    with pytest.raises(HookExecutionError):
        _runner(tmp_path, mode=PermissionMode.READ_ONLY).run(spec)


def test_no_recursion_pre_tool_hook_cannot_reenter(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    spec = HookSpec(event=HookEvent.PRE_TOOL, run="true")
    # Simulate being mid-hook: a re-entrant run must refuse.
    runner._in_hook = True  # exercising the no-recursion guard directly
    with pytest.raises(HookExecutionError, match="no re-entry"):
        runner.run(spec)


def test_fail_closed_on_timeout_and_resets_in_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook whose run_bash times out blocks the action; _in_hook resets."""
    from carve.core.agents.tools.bash_tool import BashResult

    def _timed_out(*_args: object, **_kwargs: object) -> BashResult:
        return BashResult(exit_code=-1, stdout="", truncated=False, timed_out=True)

    monkeypatch.setattr("carve.core.hooks.runner.run_bash", _timed_out)
    runner = _runner(tmp_path)
    spec = HookSpec(event=HookEvent.PRE_TOOL, run="true")
    with pytest.raises(HookExecutionError, match="timed out"):
        runner.run(spec)
    # The finally block reset the guard, so the runner is reusable.
    assert runner._in_hook is False


def test_fail_closed_on_internal_error_and_resets_in_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook whose run_bash raises internally blocks; _in_hook resets."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr("carve.core.hooks.runner.run_bash", _boom)
    runner = _runner(tmp_path)
    spec = HookSpec(event=HookEvent.PRE_TOOL, run="true")
    with pytest.raises(HookExecutionError, match="errored"):
        runner.run(spec)
    assert runner._in_hook is False


def test_wiring_match_filters_and_fires_after_gate(tmp_path: Path) -> None:
    """The pre_tool hook fires (gated) only for matching tool calls."""
    # A bash-only, command-glob hook that exits non-zero (blocks).
    spec = HookSpec(
        event=HookEvent.PRE_TOOL,
        run="false",
        match=HookMatch(tool="bash", command="git commit*"),
    )
    pre_hook, post_hook = build_tool_hooks([spec], _runner(tmp_path))
    assert pre_hook is not None
    assert post_hook is None

    # Non-matching tool: hook does not fire (no raise).
    pre_hook("read_file", {"path": "x"})
    # Matching command: hook fires, exits non-zero, blocks (raises).
    with pytest.raises(HookExecutionError):
        pre_hook("bash", {"command": "git commit -m x"})


def test_pre_tool_can_only_further_restrict(tmp_path: Path) -> None:
    """The hook fires after the gate, so it can block but never enable.

    We model "after the gate" structurally: the wiring callable raises to
    abort (further-restrict). It has no path to widen the gate's verdict —
    it only ever runs the configured command and turns a failure into an
    abort.
    """
    spec = HookSpec(event=HookEvent.PRE_TOOL, run="false")
    pre_hook, _ = build_tool_hooks([spec], _runner(tmp_path))
    assert pre_hook is not None
    with pytest.raises(HookExecutionError):
        pre_hook("bash", {"command": "git status"})
