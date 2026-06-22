"""Regression tests for the loop.py MODIFY surface.

Guards the Acceptance bar "existing loop.py machinery preserved and stays
sync" by exercising the four additions in isolation: the gate fires
*before* the tool executor (a deny means no execution), ``files_changed``
is harness-tracked from the edit/create log, the cancellation token stops
the loop between turns, and the steering queue is drained between turns —
all while the preserved retry/terminator/observer/TokenUsage paths (whose
own tests live in ``test_loop.py``) are untouched.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from carve.core.agents.cancel import CancellationToken, RunCancelled
from carve.core.agents.loop import AgentLoop
from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.agents.steering import SteeringQueue
from carve.core.agents.tools import Tool
from carve.core.agents.tools.fs_tools import make_create_file_tool


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
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
    def __init__(self, responses: list[Any]) -> None:
        self._responses = iter(responses)
        self.create_calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **_kwargs: Any) -> Any:
        self.create_calls += 1
        return next(self._responses)


def _first_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the first tool_result block across all user messages."""
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return block
    raise AssertionError("no tool_result block found in messages")


class TestGateFiresBeforeExecutor:
    def test_denied_call_does_not_execute(self, tmp_path: Path) -> None:
        executed: list[str] = []

        def _exec(_input: dict[str, Any]) -> str:
            executed.append("ran")
            return "should not happen"

        write_tool = Tool(
            name="edit",
            description="x",
            input_schema={"type": "object"},
            executor=_exec,
        )
        client = _ScriptedClient(
            [
                _response(
                    [
                        _tool_use(
                            "edit",
                            {"path": "x", "old_string": "a", "new_string": "b"},
                        )
                    ],
                    "tool_use",
                ),
                _response([_text("done")], "end_turn"),
            ]
        )
        # read_only denies edit regardless of grant.
        gate = PermissionGate(build_policy(PermissionMode.READ_ONLY))
        loop = AgentLoop(
            client,
            [write_tool],
            "sys",
            "claude-sonnet-4-5-20250929",
            gate=gate,
        )
        result = loop.run("go")
        # The executor never ran.
        assert executed == []
        # The model saw an is_error tool_result and continued to end_turn.
        assert result.text == "done"
        tool_results = _first_tool_result(result.messages)
        assert tool_results["is_error"] is True
        assert "denied" in tool_results["content"].lower()

    def test_allowed_call_executes(self, tmp_path: Path) -> None:
        client = _ScriptedClient(
            [
                _response(
                    [_tool_use("read_file", {"path": "f.py"})],
                    "tool_use",
                ),
                _response([_text("done")], "end_turn"),
            ]
        )
        (tmp_path / "f.py").write_text("hi\n")
        from carve.core.agents.m1_tools import make_read_file_tool

        gate = PermissionGate(build_policy(PermissionMode.READ_ONLY))
        loop = AgentLoop(
            client,
            [make_read_file_tool(tmp_path)],
            "sys",
            "claude-sonnet-4-5-20250929",
            gate=gate,
        )
        result = loop.run("go")
        assert result.text == "done"
        tool_result = _first_tool_result(result.messages)
        assert "is_error" not in tool_result
        assert tool_result["content"] == "hi\n"


class TestFilesChangedHarnessTracked:
    def test_files_changed_recorded_from_create(self, tmp_path: Path) -> None:
        client = _ScriptedClient(
            [
                _response(
                    [_tool_use("create_file", {"path": "new.py", "content": "y\n"})],
                    "tool_use",
                ),
                _response([_text("done")], "end_turn"),
            ]
        )
        gate = PermissionGate(build_policy(PermissionMode.BUILD))
        loop = AgentLoop(
            client,
            [make_create_file_tool(tmp_path)],
            "sys",
            "claude-sonnet-4-5-20250929",
            gate=gate,
        )
        loop.run("go")
        assert loop.files_changed == ["new.py"]
        assert (tmp_path / "new.py").read_text() == "y\n"

    def test_files_changed_empty_when_no_writes(self, tmp_path: Path) -> None:
        client = _ScriptedClient([_response([_text("nothing")], "end_turn")])
        loop = AgentLoop(client, [], "sys", "claude-sonnet-4-5-20250929")
        loop.run("go")
        assert loop.files_changed == []


class TestCancellationBetweenTurns:
    def test_cancel_stops_loop_between_turns(self) -> None:
        token = CancellationToken()

        # A tool that trips the cancel mid-run, so the *next* between-turns
        # check raises rather than making another API call.
        def _exec(_input: dict[str, Any]) -> str:
            token.cancel()
            return "ok"

        trip = Tool(
            name="trip",
            description="x",
            input_schema={"type": "object"},
            executor=_exec,
        )
        client = _ScriptedClient(
            [
                _response([_tool_use("trip", {})], "tool_use"),
                # If the loop didn't stop, this would be requested.
                _response([_text("should-not-reach")], "end_turn"),
            ]
        )
        loop = AgentLoop(
            client,
            [trip],
            "sys",
            "claude-sonnet-4-5-20250929",
            cancellation=token,
        )
        with pytest.raises(RunCancelled):
            loop.run("go")
        # Only the first turn's API call happened.
        assert client.create_calls == 1

    def test_precancelled_token_stops_before_first_call(self) -> None:
        token = CancellationToken()
        token.cancel()
        client = _ScriptedClient([_response([_text("x")], "end_turn")])
        loop = AgentLoop(client, [], "sys", "claude-sonnet-4-5-20250929", cancellation=token)
        with pytest.raises(RunCancelled):
            loop.run("go")
        assert client.create_calls == 0


class TestSteeringBetweenTurns:
    def test_steering_message_appended_before_next_turn(self) -> None:
        queue = SteeringQueue()

        def _exec(_input: dict[str, Any]) -> str:
            # Inject guidance mid-run; it should land before the next turn.
            queue.push("focus on the edge cases")
            return "ok"

        tool = Tool(
            name="noop",
            description="x",
            input_schema={"type": "object"},
            executor=_exec,
        )
        client = _ScriptedClient(
            [
                _response([_tool_use("noop", {})], "tool_use"),
                _response([_text("done")], "end_turn"),
            ]
        )
        loop = AgentLoop(
            client,
            [tool],
            "sys",
            "claude-sonnet-4-5-20250929",
            steering=queue,
        )
        result = loop.run("go")
        assert result.text == "done"
        # The steering message is in the conversation as a user message.
        user_texts = [
            m["content"]
            for m in result.messages
            if m["role"] == "user" and isinstance(m["content"], str)
        ]
        assert "focus on the edge cases" in user_texts


class TestLoopStaysSync:
    def test_loop_run_is_not_a_coroutine(self) -> None:
        # The Acceptance bar: the loop stays sync. `run` must be a plain
        # function, not `async def`.
        assert not inspect.iscoroutinefunction(AgentLoop.run)
        assert not inspect.iscoroutinefunction(AgentLoop._execute_tool_calls)

    def test_no_asyncio_imported_in_loop_module(self) -> None:
        # `loop.py` must not pull asyncio in (the threadpool wrapping lives
        # in the serve layer, not here).
        import carve.core.agents.loop as loop_module

        source = Path(loop_module.__file__).read_text()
        assert "import asyncio" not in source
        # Guard against the symbol being present via re-import.
        assert getattr(loop_module, "asyncio", None) is None
        # asyncio is imported in this test file deliberately to assert the
        # loop does not depend on it; reference it so the import is "used".
        assert asyncio is not None
