"""Unit tests for the ``post_build`` lifecycle hook wiring.

`build_post_build_hook` is the lifecycle analogue of `build_tool_hooks`: it
selects the `POST_BUILD` specs, expands the event payload into each command,
and runs them through the SAME gated `HookRunner` (at BUILD) a tool hook
uses — so the bash gate clamp/deny is identical. `_expand_lifecycle`
substitutes only the payload's own keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.hooks.config import HookMatch, HookSpec
from carve.core.hooks.events import HookEvent
from carve.core.hooks.runner import HookExecutionError, HookRunner
from carve.core.hooks.wiring import (
    _expand_lifecycle,
    build_post_build_hook,
)


def _runner(tmp_path: Path, mode: PermissionMode = PermissionMode.BUILD) -> HookRunner:
    return HookRunner(gate=PermissionGate(build_policy(mode)), project_dir=tmp_path)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pipeline_name": "stripe",
        "build_id": "build_abc",
        "target": "dev",
        "plan_id": "plan_1",
        "files": ["el/stripe/main.py"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- selection


def test_returns_none_when_no_post_build_spec(tmp_path: Path) -> None:
    """A spec set with no POST_BUILD entry yields None (build flow skips it)."""
    specs = [
        HookSpec(event=HookEvent.PRE_TOOL, run="true"),
        HookSpec(event=HookEvent.POST_TOOL, run="true"),
    ]
    assert build_post_build_hook(specs, _runner(tmp_path)) is None


def test_selects_only_post_build_specs(tmp_path: Path) -> None:
    """Only POST_BUILD specs are fired; a non-matching event spec is ignored."""
    # A POST_BUILD hook (`true` → ok) plus a PRE_TOOL hook that would BLOCK if
    # it ran (`false`). The lifecycle hook must run ONLY the post_build one.
    specs = [
        HookSpec(event=HookEvent.PRE_TOOL, run="false"),
        HookSpec(event=HookEvent.POST_BUILD, run="true"),
    ]
    hook = build_post_build_hook(specs, _runner(tmp_path))
    assert hook is not None
    # No raise → only the `true` post_build spec ran (the `false` pre_tool
    # spec was not selected; had it run it would have raised).
    hook(_payload())


# --------------------------------------------------------------- expansion


def test_expand_lifecycle_substitutes_only_payload_keys() -> None:
    template = "echo {pipeline_name} {build_id} {target} {plan_id}"
    out = _expand_lifecycle(template, _payload())
    assert out == "echo stripe build_abc dev plan_1"


def test_expand_lifecycle_leaves_unknown_placeholders_untouched() -> None:
    # An unknown placeholder is not a payload key → left verbatim (it then
    # fails the bash gate or simply prints literally; it cannot inject).
    out = _expand_lifecycle("echo {pipeline_name} {not_a_key}", _payload())
    assert out == "echo stripe {not_a_key}"


def test_expand_lifecycle_renders_list_files_space_joined() -> None:
    # `{files}` is a list payload key — it renders space-joined (the natural
    # shell form `el/a.py el/b.py`), NOT a Python list repr `['el/a.py', ...]`
    # a hook author would never want in a command.
    payload = _payload(files=["el/stripe/main.py", "el/stripe/requirements.txt"])
    out = _expand_lifecycle("echo {files}", payload)
    assert out == "echo el/stripe/main.py el/stripe/requirements.txt"


def test_expanded_command_runs_through_the_runner(tmp_path: Path) -> None:
    """The expanded command actually executes via the gated runner (exit 0)."""
    spec = HookSpec(event=HookEvent.POST_BUILD, run="echo {pipeline_name}")
    hook = build_post_build_hook([spec], _runner(tmp_path))
    assert hook is not None
    # `echo stripe` is allow-listed at BUILD and exits 0 → no raise. Proves the
    # payload-expanded command ran through the gated runner (the substitution
    # itself is asserted in `test_expand_lifecycle_substitutes_only_payload_keys`).
    hook(_payload())


# ------------------------------------------------------- gate (fail-closed)


def test_metacharacter_command_denied_by_bash_gate(tmp_path: Path) -> None:
    """A post_build command with $()/;/&& is denied by the same bash gate."""
    for cmd in ("echo hi; rm -rf /", "echo $(whoami)", "echo a && echo b"):
        spec = HookSpec(event=HookEvent.POST_BUILD, run=cmd)
        hook = build_post_build_hook([spec], _runner(tmp_path))
        assert hook is not None
        with pytest.raises(HookExecutionError, match="denied by the bash gate"):
            hook(_payload())


def test_expansion_cannot_smuggle_metacharacters_past_the_gate(tmp_path: Path) -> None:
    """An expanded payload value carrying a metacharacter is still gated."""
    spec = HookSpec(event=HookEvent.POST_BUILD, run="echo {pipeline_name}")
    hook = build_post_build_hook([spec], _runner(tmp_path))
    assert hook is not None
    # The payload value itself contains `;` — after expansion the command is
    # `echo stripe; rm -rf /`, which the gate denies (the expansion did not
    # bypass the metacharacter screen).
    with pytest.raises(HookExecutionError, match="denied by the bash gate"):
        hook(_payload(pipeline_name="stripe; rm -rf /"))


def test_raising_command_propagates_hook_execution_error(tmp_path: Path) -> None:
    """A non-zero post_build command raises HookExecutionError out of the hook.

    The build flow (not this unit) decides the post-commit semantics; the
    callable itself propagates, mirroring the tool-hook callable.
    """
    spec = HookSpec(event=HookEvent.POST_BUILD, run="false")
    hook = build_post_build_hook([spec], _runner(tmp_path))
    assert hook is not None
    with pytest.raises(HookExecutionError, match="non-zero"):
        hook(_payload())


def test_gate_clamps_post_build_to_build_mode(tmp_path: Path) -> None:
    """The runner is fed the BUILD policy → a write subcommand is allowed at
    BUILD but a network egress program is still denied (no bypass)."""
    spec = HookSpec(event=HookEvent.POST_BUILD, run="curl http://evil")
    hook = build_post_build_hook([spec], _runner(tmp_path, mode=PermissionMode.BUILD))
    assert hook is not None
    with pytest.raises(HookExecutionError, match="denied by the bash gate"):
        hook(_payload())


def test_match_filter_is_inert_for_post_build(tmp_path: Path) -> None:
    """A `match` on a post_build hook is ignored (no tool/command to match)."""
    # A match that could never match a tool call still fires for post_build,
    # because lifecycle hooks have no tool/command to filter on.
    spec = HookSpec(
        event=HookEvent.POST_BUILD,
        run="true",
        match=HookMatch(tool="bash", command="git commit*"),
    )
    hook = build_post_build_hook([spec], _runner(tmp_path))
    assert hook is not None
    # No raise → the hook fired despite the (inert) match filter.
    hook(_payload())
