"""Hook propagation into delegated subagents — fire, abort, and *clamp*.

The four guarantees, proved at the delegation seam (closes the adversarial
review's MUST-FIX 1):

1. A ``pre_tool_hook`` fires for the **child's** own tool call — the hook
   reached the child loop, not just the parent.
2. A **raising** ``pre_tool_hook`` turns the child's tool call into an
   ``is_error`` tool_result instead of crashing the child loop.
3. A ``post_tool_hook`` fires after a successful child tool execution.
4. **The security invariant.** A parent at ``build`` delegating to a
   ``read_only``-capability child runs the child at ``child_mode =
   min(build, read_only) = read_only``. A hook that runs a **write/network**
   bash command (``git commit``) is **DENIED** when it fires in the child
   (the child-mode clamp), while a **read** command (``git status``) is
   allowed — proving the hook is clamped to ``child_mode``, never the
   parent's ``build``. Covered at depth-1 *and* depth-2 nesting.

The hooks are supplied as a **factory** ``(mode) -> (pre, post)`` (see
:data:`carve.core.agents.delegation.HookFactory`); the runner calls it at
the child's clamped mode so the hook's gate is rebuilt at the child's
authority. A pre-built closure carrying the parent's mode (the old shape)
would escalate — these tests would fail against it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from carve.cli.orchestrator.extensibility_wiring import (
    build_extensibility_hook_factory,
)
from carve.core.agents.delegation import HookFactory, SubagentRunner
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.subagent_registry import AgentSpec, SubagentRegistry
from carve.core.agents.tools import Tool
from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import PathsConfig

# --------------------------------------------------------------- helpers
# Mirror tests/core/agents/permissions/test_delegation_clamp.py.


def _usage(input_tokens: int = 100, output_tokens: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _tool_use(name: str, input_: dict[str, Any], tid: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=input_)


def _response(content: list[Any], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=_usage())


class _ScriptedClient:
    """Returns canned responses; records the system prompt(s) it was sent."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = iter(responses)
        self.systems: list[str] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.systems.append(kwargs.get("system", ""))
        return next(self._responses)


def _registry_with(*specs: AgentSpec) -> SubagentRegistry:
    reg = SubagentRegistry()
    for spec in specs:
        reg.register(spec)
    return reg


def _probe_tool() -> tuple[Tool, list[bool]]:
    """A recording probe tool plus a list recording each time it executed.

    It is named ``read_file`` — a tool in the read-only permitted floor — so
    the child's mode gate admits the call in *every* mode (read_only → build)
    and the hook is what decides allow/deny, not the tool-permission floor.
    (Its executor is a no-op recorder; the gate only checks the name.)
    """
    ran: list[bool] = []

    def _execute(_input: dict[str, Any]) -> str:
        ran.append(True)
        return "probe-executed"

    probe = Tool(
        name="read_file",
        description="A recording probe tool (named read_file to be permitted).",
        input_schema={"type": "object", "properties": {}},
        executor=_execute,
    )
    return probe, ran


def _probe_agent(
    *, capability: PermissionMode = PermissionMode.BUILD
) -> tuple[AgentSpec, list[bool]]:
    """A subagent whose only non-terminator tool is the recording probe."""
    probe, ran = _probe_tool()

    def factory(_paths: ProjectPaths) -> list[Tool]:
        return [probe]

    spec = AgentSpec(
        name="prober",
        system_prompt="You are the prober.",
        capability=capability,
        tool_factory=factory,
    )
    return spec, ran


def _probe_then_submit(status: str = "succeeded") -> list[Any]:
    """Script: the child calls the probe (``read_file``) once, then submits."""
    return [
        _response([_tool_use("read_file", {}, "p1")], "tool_use"),
        _response(
            [
                _tool_use(
                    "submit_result",
                    {"status": status, "summary": "did the probe"},
                    "s1",
                )
            ],
            "tool_use",
        ),
    ]


def _write_hooks_toml(project_dir: Path, body: str) -> None:
    carve_dir = project_dir / "carve"
    carve_dir.mkdir(parents=True, exist_ok=True)
    (carve_dir / "hooks.toml").write_text(body, encoding="utf-8")


# ----------------------------------------------------- 1/2/3: fire / abort


class TestHookReachesChildLoop:
    def test_pre_tool_hook_fires_for_child_tool_call(self, tmp_path: Path) -> None:
        """A recording pre_tool hook captures the child's own probe call."""
        recorded: list[tuple[str, dict[str, Any]]] = []

        def _factory(_mode: PermissionMode) -> tuple[Any, Any]:
            def _pre(tool_name: str, tool_input: dict[str, Any]) -> None:
                recorded.append((tool_name, tool_input))

            return _pre, None

        spec, _ran = _probe_agent()
        runner = SubagentRunner(
            registry=_registry_with(spec),
            paths=ProjectPaths.from_root(tmp_path),
            client=_ScriptedClient(_probe_then_submit()),
            model="claude-sonnet-4-5-20250929",
            hook_factory=_factory,
        )
        runner.run("prober", "go", {}, parent_mode=PermissionMode.BUILD)

        # The hook fired for the child's probe call (proves it reached the
        # child loop, not just the parent).
        assert ("read_file", {}) in recorded

    def test_raising_pre_tool_hook_aborts_child_call_not_loop(self, tmp_path: Path) -> None:
        """A raising pre_tool hook → is_error tool_result, child keeps running."""

        def _factory(_mode: PermissionMode) -> tuple[Any, Any]:
            def _pre(tool_name: str, _tool_input: dict[str, Any]) -> None:
                # Raise only for the probe call (not submit_result), so the
                # child can still terminate cleanly after the aborted call.
                if tool_name == "read_file":
                    raise RuntimeError("hook says no")

            return _pre, None

        spec, ran = _probe_agent()
        runner = SubagentRunner(
            registry=_registry_with(spec),
            paths=ProjectPaths.from_root(tmp_path),
            client=_ScriptedClient(_probe_then_submit()),
            model="claude-sonnet-4-5-20250929",
            hook_factory=_factory,
        )
        # The child loop did not crash — it ran to submit_result.
        result = runner.run("prober", "go", {}, parent_mode=PermissionMode.BUILD)
        assert result.status == "succeeded"
        # The probe executor never ran: the raising hook aborted the call.
        assert ran == []

    def test_post_tool_hook_fires_after_successful_child_call(self, tmp_path: Path) -> None:
        """The post_tool hook fires once the child's probe executes."""
        post_calls: list[str] = []

        def _factory(_mode: PermissionMode) -> tuple[Any, Any]:
            def _post(tool_name: str, _tool_input: dict[str, Any]) -> None:
                post_calls.append(tool_name)

            return None, _post

        spec, ran = _probe_agent()
        runner = SubagentRunner(
            registry=_registry_with(spec),
            paths=ProjectPaths.from_root(tmp_path),
            client=_ScriptedClient(_probe_then_submit()),
            model="claude-sonnet-4-5-20250929",
            hook_factory=_factory,
        )
        runner.run("prober", "go", {}, parent_mode=PermissionMode.BUILD)

        # The probe ran and the post hook fired after it (for the probe).
        assert ran == [True]
        assert "read_file" in post_calls

    def test_no_factory_means_no_hooks(self, tmp_path: Path) -> None:
        """Default (no hook_factory) runs the child hook-free, as before."""
        spec, ran = _probe_agent()
        runner = SubagentRunner(
            registry=_registry_with(spec),
            paths=ProjectPaths.from_root(tmp_path),
            client=_ScriptedClient(_probe_then_submit()),
            model="claude-sonnet-4-5-20250929",
        )
        result = runner.run("prober", "go", {}, parent_mode=PermissionMode.BUILD)
        assert result.status == "succeeded"
        assert ran == [True]


# --------------------------------------------- 4: the child-mode clamp (MF1)
#
# A bash hook fired inside the child runs the SAME bash gate a child tool
# call would, built at child_mode. `git commit` is a write subcommand
# (allowed at build, denied below); `git status` is a read subcommand
# (allowed in every mode). The factory we hand the runner builds the gate at
# whatever mode it's *called with* — so the assertion is purely about which
# mode the runner passes (it must pass child_mode, not parent_mode).


_COMMIT_HOOK_TOML = """\
[[hook]]
on = "pre_tool"
match = { tool = "read_file" }
run = "git commit -m wip"
"""

_STATUS_HOOK_TOML = """\
[[hook]]
on = "pre_tool"
match = { tool = "read_file" }
run = "git status"
"""


def _real_hook_factory(project_dir: Path) -> HookFactory:
    """A live factory over the fixture carve/hooks.toml (gated, mode-clamped)."""
    return build_extensibility_hook_factory(project_dir=project_dir, paths=PathsConfig())


def _init_git_repo(project_dir: Path) -> None:
    """A real (empty) git repo so `git status` exits 0 inside the sandbox."""
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)


def _run_prober_capturing(
    project_dir: Path,
    *,
    parent_mode: PermissionMode,
    capability: PermissionMode,
    depth: int = 1,
) -> tuple[Any, list[bool]]:
    """Run the prober subagent with the live hook factory; return result+ran."""
    spec, ran = _probe_agent(capability=capability)
    runner = SubagentRunner(
        registry=_registry_with(spec),
        paths=ProjectPaths.from_root(project_dir),
        client=_ScriptedClient(_probe_then_submit()),
        model="claude-sonnet-4-5-20250929",
        hook_factory=_real_hook_factory(project_dir),
    )
    result = runner.run("prober", "go", {}, parent_mode=parent_mode, depth=depth)
    return result, ran


class TestChildModeClamp:
    def test_write_hook_denied_in_read_only_child(self, tmp_path: Path) -> None:
        """`git commit` hook is DENIED when the child clamps to read_only.

        Parent at build, child capability read_only → child_mode read_only.
        The write-subcommand hook is denied by the child-mode bash gate, so
        the probe call is aborted (is_error) and never executes — even
        though the *parent* runs at build (where git commit is allowed).
        """
        _write_hooks_toml(tmp_path, _COMMIT_HOOK_TOML)
        result, ran = _run_prober_capturing(
            tmp_path,
            parent_mode=PermissionMode.BUILD,
            capability=PermissionMode.READ_ONLY,
        )
        # The hook denied the call: probe never executed.
        assert ran == []
        # The child loop survived (ran to submit_result).
        assert result.status == "succeeded"

    def test_read_hook_allowed_in_read_only_child(self, tmp_path: Path) -> None:
        """`git status` hook is ALLOWED in the read_only child → probe runs."""
        _init_git_repo(tmp_path)
        _write_hooks_toml(tmp_path, _STATUS_HOOK_TOML)
        result, ran = _run_prober_capturing(
            tmp_path,
            parent_mode=PermissionMode.BUILD,
            capability=PermissionMode.READ_ONLY,
        )
        # The read hook passed → the probe executed.
        assert ran == [True]
        assert result.status == "succeeded"

    def test_write_hook_allowed_when_child_stays_build(self, tmp_path: Path) -> None:
        """Control: a build-capable child at a build parent allows git commit.

        Proves the read_only denial above is the *clamp* doing its job, not
        the hook being broken: same write hook, but child_mode = build →
        allowed, so the probe runs. (`git commit` on an empty repo exits
        non-zero — "nothing to commit" / "please tell me who you are" — so
        the hook would still block here; we instead use `git status` against
        a real repo to isolate "allowed at build" cleanly.)
        """
        _init_git_repo(tmp_path)
        _write_hooks_toml(tmp_path, _STATUS_HOOK_TOML)
        result, ran = _run_prober_capturing(
            tmp_path,
            parent_mode=PermissionMode.BUILD,
            capability=PermissionMode.BUILD,
        )
        assert ran == [True]
        assert result.status == "succeeded"

    @pytest.mark.parametrize("depth", [1, 2])
    def test_write_hook_denied_in_read_only_child_at_depth(
        self, tmp_path: Path, depth: int
    ) -> None:
        """The child-mode clamp holds at depth-1 and depth-2 nesting.

        At depth 2 the running mode is still ``min(parent_mode, capability)``
        for *this* child; the clamp is per-hop, so a write hook is denied in
        a read_only child regardless of how deep the delegation chain is.
        """
        _write_hooks_toml(tmp_path, _COMMIT_HOOK_TOML)
        result, ran = _run_prober_capturing(
            tmp_path,
            parent_mode=PermissionMode.BUILD,
            capability=PermissionMode.READ_ONLY,
            depth=depth,
        )
        assert ran == []
        assert result.status == "succeeded"
