"""The executor binder: grant names -> real tools (stubs only), with injection.

Plus an end-to-end check that a delegated declarative agent actually RUNS a
real tool (the gap this seam closes — before it, every declarative grant raised).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from carve.core.agents.delegation import SubagentRunner
from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.agents.subagent_registry import (
    AgentSpec,
    SubagentRegistry,
    grant_stub_tool,
    is_grant_stub,
)
from carve.core.agents.tool_binding import BindingContext, bind_grant_tools
from carve.core.agents.tools import Tool
from carve.core.config.paths import ProjectPaths


def _ctx(root: Path, **kw: Any) -> BindingContext:
    gate = PermissionGate(build_policy(PermissionMode.BUILD))
    return BindingContext(project_dir=root, gate=gate, **kw)


def _stubs(*names: str) -> list[Tool]:
    return [grant_stub_tool(n) for n in names]


# --- unit: binding ---------------------------------------------------------


def test_base_tools_bind_to_real_executors(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello world\n")
    bound = {
        t.name: t for t in bind_grant_tools(_stubs("read_file", "grep", "glob"), _ctx(tmp_path))
    }
    assert not is_grant_stub(bound["read_file"])  # really bound
    assert "hello world" in str(bound["read_file"].executor({"path": "a.txt"}))


def test_edit_tool_actually_edits(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("before\n")
    (edit,) = bind_grant_tools(_stubs("edit"), _ctx(tmp_path))
    edit.executor({"path": "f.txt", "old_string": "before", "new_string": "after"})
    assert f.read_text() == "after\n"


def test_bash_binds_with_the_gate(tmp_path: Path) -> None:
    (bash,) = bind_grant_tools(_stubs("bash"), _ctx(tmp_path))
    assert bash.name == "bash" and not is_grant_stub(bash)
    out = bash.executor({"command": "echo hi"})
    assert "hi" in str(out)


def test_unbound_grant_keeps_raising_stub(tmp_path: Path) -> None:
    # `sql` needs a connection the harness doesn't hold; with nothing injected
    # it stays a stub that fails loud (never a silent no-op).
    (sql,) = bind_grant_tools(_stubs("sql"), _ctx(tmp_path))
    assert is_grant_stub(sql)
    with pytest.raises(RuntimeError):
        sql.executor({"op": "run"})


def test_extra_tools_injection_supplies_unbindable_names(tmp_path: Path) -> None:
    injected = Tool(
        name="sql",
        description="injected",
        input_schema={"type": "object"},
        executor=lambda _i: {"ran": True},
    )
    (sql,) = bind_grant_tools(_stubs("sql"), _ctx(tmp_path, extra_tools={"sql": injected}))
    assert sql is injected
    assert sql.executor({"op": "run"}) == {"ran": True}


def test_injected_tool_with_mismatched_name_fails_loud(tmp_path: Path) -> None:
    # A caller injecting a tool whose .name != the grant key is a wiring bug;
    # bind loud rather than silently breaking the grant.
    wrong = Tool(
        name="not_sql",
        description="x",
        input_schema={"type": "object"},
        executor=lambda _i: {},
    )
    with pytest.raises(ValueError, match="mismatched name"):
        bind_grant_tools(_stubs("sql"), _ctx(tmp_path, extra_tools={"sql": wrong}))


def test_real_factory_tool_passes_through_untouched(tmp_path: Path) -> None:
    # A spec/test-fixture that already provides a real tool must not be replaced.
    real = Tool(
        name="read_file",
        description="custom probe",
        input_schema={"type": "object"},
        executor=lambda _i: "probe",
    )
    (out,) = bind_grant_tools([real], _ctx(tmp_path))
    assert out is real  # not rebound to the base read_file


def test_dedup_by_name(tmp_path: Path) -> None:
    bound = bind_grant_tools(_stubs("grep", "grep", "glob"), _ctx(tmp_path))
    assert [t.name for t in bound] == ["grep", "glob"]


# --- integration: a delegated declarative agent runs a real tool -----------


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=1, output_tokens=1)


def _tool_use(name: str, input_: dict[str, Any], tid: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=input_)


def _response(content: list[Any], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=_usage())


class _ScriptedClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = iter(responses)
        self.messages = SimpleNamespace(create=lambda **kw: next(self._responses))


def _declarative_spec(*grant_names: str) -> AgentSpec:
    """A spec whose tool_factory yields name-only grant stubs (declarative)."""
    return AgentSpec(
        name="worker",
        system_prompt="You are the worker.",
        capability=PermissionMode.BUILD,
        tool_factory=lambda _paths: _stubs(*grant_names),
    )


def test_delegated_declarative_agent_runs_a_bound_tool(tmp_path: Path) -> None:
    # Before the binder, the agent's `edit` grant raised on first call. Now a
    # delegated declarative agent edits a real file.
    f = tmp_path / "code.py"
    f.write_text("x = 1\n")

    reg = SubagentRegistry()
    reg.register(_declarative_spec("edit"))

    script = [
        _response(
            [_tool_use("edit", {"path": "code.py", "old_string": "x = 1", "new_string": "x = 2"})],
            "tool_use",
        ),
        _response(
            [_tool_use("submit_result", {"status": "succeeded", "summary": "done"}, "s1")],
            "tool_use",
        ),
    ]
    runner = SubagentRunner(
        registry=reg,
        paths=ProjectPaths.from_root(tmp_path),
        client=_ScriptedClient(script),
        model="claude-opus-4-8",
    )
    result = runner.run("worker", "make x = 2", {}, parent_mode=PermissionMode.BUILD)
    assert f.read_text() == "x = 2\n"  # the bound edit tool actually ran
    assert "code.py" in result.files_changed


def test_injected_tool_runs_through_delegation(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    injected = Tool(
        name="sql",
        description="injected sql",
        input_schema={"type": "object"},
        executor=lambda i: calls.append(i) or {"rows": [{"n": 1}]},
    )
    reg = SubagentRegistry()
    reg.register(_declarative_spec("sql"))
    script = [
        _response([_tool_use("sql", {"op": "run", "sql": "SELECT 1"})], "tool_use"),
        _response(
            [_tool_use("submit_result", {"status": "succeeded", "summary": "ok"}, "s1")], "tool_use"
        ),
    ]
    runner = SubagentRunner(
        registry=reg,
        paths=ProjectPaths.from_root(tmp_path),
        client=_ScriptedClient(script),
        model="claude-opus-4-8",
        extra_tools={"sql": injected},
    )
    runner.run("worker", "run a query", {}, parent_mode=PermissionMode.BUILD)
    assert calls == [{"op": "run", "sql": "SELECT 1"}]  # injected sql actually executed
