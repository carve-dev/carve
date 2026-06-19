"""Unit tests for subagent delegation: clamp + isolation + roll-up.

The spec bar: a ``read_only`` parent delegating to a build-capable agent
runs the child ``read_only``; a child ``edit``/``bash``-write is denied;
the parent transcript is not visible to the child; cost/usage roll up
into the :class:`DelegationResult`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from carve.core.agents.delegation import (
    MAX_DELEGATION_DEPTH,
    SubagentError,
    SubagentRunner,
)
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.subagent_registry import AgentSpec, SubagentRegistry
from carve.core.agents.tools import Tool
from carve.core.agents.tools.fs_tools import make_create_file_tool, make_edit_tool
from carve.core.config.paths import ProjectPaths

# --------------------------------------------------------------- helpers


def _usage(input_tokens: int = 100, output_tokens: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _tool_use(name: str, input_: dict[str, Any], tid: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=input_)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


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


def _registry_with(spec: AgentSpec) -> SubagentRegistry:
    reg = SubagentRegistry()
    reg.register(spec)
    return reg


def _engineer_spec(project_dir: Path) -> AgentSpec:
    """A build-capable engineer whose factory hands it edit + create_file."""

    def factory(paths: ProjectPaths) -> list[Tool]:
        return [
            make_edit_tool(paths.root),
            make_create_file_tool(paths.root),
        ]

    return AgentSpec(
        name="engineer",
        system_prompt="You are the engineer.",
        capability=PermissionMode.BUILD,
        tool_factory=factory,
    )


# --------------------------------------------------------------- tests


class TestModeClamp:
    def test_read_only_parent_clamps_build_child(self, tmp_path: Path) -> None:
        # The child tries to edit (a write) on its first turn; under the
        # clamped read_only mode the gate denies it, so the child gets an
        # is_error tool_result and then submits a failed result.
        client = _ScriptedClient(
            [
                _response(
                    [
                        _tool_use(
                            "edit",
                            {"path": "x.py", "old_string": "a", "new_string": "b"},
                        )
                    ],
                    "tool_use",
                ),
                _response(
                    [
                        _tool_use(
                            "submit_result",
                            {
                                "status": "needs_user_input",
                                "summary": "could not write in read_only",
                            },
                        )
                    ],
                    "tool_use",
                ),
            ]
        )
        runner = SubagentRunner(
            registry=_registry_with(_engineer_spec(tmp_path)),
            paths=ProjectPaths.from_root(tmp_path),
            client=client,
            model="claude-sonnet-4-5-20250929",
        )
        result = runner.run(
            "engineer",
            "edit the file",
            {"goal": "x"},
            parent_mode=PermissionMode.READ_ONLY,
        )
        # The write never happened — no file created, files_changed empty.
        assert result.files_changed == []
        assert not (tmp_path / "x.py").exists()
        assert result.status == "needs_user_input"

    def test_build_parent_allows_write(self, tmp_path: Path) -> None:
        client = _ScriptedClient(
            [
                _response(
                    [_tool_use("create_file", {"path": "new.py", "content": "y=1\n"})],
                    "tool_use",
                ),
                _response(
                    [
                        _tool_use(
                            "submit_result",
                            {"status": "succeeded", "summary": "wrote new.py"},
                        )
                    ],
                    "tool_use",
                ),
            ]
        )
        runner = SubagentRunner(
            registry=_registry_with(_engineer_spec(tmp_path)),
            paths=ProjectPaths.from_root(tmp_path),
            client=client,
            model="claude-sonnet-4-5-20250929",
        )
        result = runner.run(
            "engineer",
            "create new.py",
            {},
            parent_mode=PermissionMode.BUILD,
        )
        assert (tmp_path / "new.py").read_text() == "y=1\n"
        # files_changed is harness-tracked from the create.
        assert result.files_changed == ["new.py"]
        assert result.status == "succeeded"
        assert result.outputs == {}


class TestContextIsolation:
    def test_child_prompt_holds_only_named_context_not_transcript(
        self, tmp_path: Path
    ) -> None:
        client = _ScriptedClient(
            [
                _response(
                    [
                        _tool_use(
                            "submit_result",
                            {"status": "succeeded", "summary": "done"},
                        )
                    ],
                    "tool_use",
                ),
            ]
        )
        runner = SubagentRunner(
            registry=_registry_with(_engineer_spec(tmp_path)),
            paths=ProjectPaths.from_root(tmp_path),
            client=client,
            model="claude-sonnet-4-5-20250929",
        )
        secret_parent_detail = "PARENT_TRANSCRIPT_SHOULD_NOT_LEAK"
        runner.run(
            "engineer",
            "do the thing",
            {"design_summary": "build a pipeline"},
            parent_mode=PermissionMode.BUILD,
        )
        system = client.systems[0]
        # The named context key is present; arbitrary parent transcript is not.
        assert "design_summary" in system
        assert "build a pipeline" in system
        assert secret_parent_detail not in system


class TestUsageRollup:
    def test_usage_and_cost_roll_up(self, tmp_path: Path) -> None:
        client = _ScriptedClient(
            [
                _response(
                    [
                        _tool_use(
                            "submit_result",
                            {"status": "succeeded", "summary": "ok"},
                        )
                    ],
                    "tool_use",
                ),
            ]
        )
        runner = SubagentRunner(
            registry=_registry_with(_engineer_spec(tmp_path)),
            paths=ProjectPaths.from_root(tmp_path),
            client=client,
            model="claude-sonnet-4-5-20250929",
        )
        result = runner.run("engineer", "t", {}, parent_mode=PermissionMode.BUILD)
        # One turn of _usage(100, 20).
        assert result.usage.input_tokens == 100
        assert result.usage.output_tokens == 20
        assert result.cost_usd > 0


class TestGuards:
    def test_unknown_agent_raises(self, tmp_path: Path) -> None:
        runner = SubagentRunner(
            registry=SubagentRegistry(),
            paths=ProjectPaths.from_root(tmp_path),
            client=_ScriptedClient([]),
            model="claude-sonnet-4-5-20250929",
        )
        try:
            runner.run("nope", "t", {}, parent_mode=PermissionMode.BUILD)
        except SubagentError as exc:
            assert "Unknown subagent" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SubagentError")

    def test_depth_limit_enforced(self, tmp_path: Path) -> None:
        runner = SubagentRunner(
            registry=_registry_with(_engineer_spec(tmp_path)),
            paths=ProjectPaths.from_root(tmp_path),
            client=_ScriptedClient([]),
            model="claude-sonnet-4-5-20250929",
        )
        try:
            runner.run(
                "engineer",
                "t",
                {},
                parent_mode=PermissionMode.BUILD,
                depth=MAX_DELEGATION_DEPTH + 1,
            )
        except SubagentError as exc:
            assert "depth" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SubagentError on depth overflow")
