"""Unit tests for `AgentLoop` with a mocked Anthropic client.

The Anthropic SDK's response objects are pydantic-style models, but the
loop only reads attributes (`stop_reason`, `usage`, `content`, etc.) so
`SimpleNamespace` is a drop-in stand-in. We never hit the real API.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from anthropic import APIStatusError, BadRequestError, RateLimitError

from carve.core.agents.exceptions import (
    AgentError,
    InvalidRequestError,
    MaxTurnsExceeded,
    RateLimitExhausted,
    UnexpectedStopReason,
)
from carve.core.agents.loop import (
    AgentLoop,
    TokenUsage,
    load_m1_code_agent_prompt,
)
from carve.core.agents.tools import Tool, ToolExecutionError

# ----------------------------------------------------------- response helpers


def _usage(input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(
    name: str, input_: dict[str, Any], tool_id: str = "tu_1"
) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _response(
    *,
    content: list[Any],
    stop_reason: str,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=usage if usage is not None else _usage(),
    )


def _client_returning(*responses: Any) -> MagicMock:
    """Build a mock client that records per-call snapshots of `messages`.

    `MagicMock.call_args_list` stores references, not copies — and the
    loop mutates its `messages` list in place between calls. We capture
    a deep copy of `messages` on each call and stash the list of
    snapshots on `client.messages_per_call`.
    """
    import copy

    client = MagicMock()
    snapshots: list[list[dict[str, Any]]] = []
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        snapshots.append(copy.deepcopy(kwargs.get("messages", [])))
        return next(response_iter)

    client.messages.create.side_effect = _create
    client.messages_per_call = snapshots
    return client


# ---------------------------------------------------------------- echo tool


def _echo_tool(record: list[dict[str, Any]] | None = None) -> Tool:
    """A trivial tool that echoes its input back to the agent."""
    captured: list[dict[str, Any]] = record if record is not None else []

    def _execute(input_: dict[str, Any]) -> str:
        captured.append(dict(input_))
        return f"echo: {input_.get('msg', '')}"

    return Tool(
        name="echo",
        description="Echo a message back.",
        input_schema={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
        executor=_execute,
    )


def _failing_tool() -> Tool:
    def _execute(input_: dict[str, Any]) -> str:
        raise ToolExecutionError("boom")

    return Tool(
        name="boom",
        description="Always fails.",
        input_schema={"type": "object", "properties": {}},
        executor=_execute,
    )


# ----------------------------------------------------------------- end_turn


class TestEndTurn:
    def test_returns_text_immediately(self) -> None:
        client = _client_returning(
            _response(content=[_text_block("done")], stop_reason="end_turn"),
        )
        loop = AgentLoop(
            client=client,
            tools=[],
            system_prompt="sys",
            model="claude-sonnet-4-5-20250929",
        )
        result = loop.run("hello")
        assert result.text == "done"
        assert result.turns == 1
        assert result.token_usage.input_tokens == 10
        assert result.token_usage.output_tokens == 5

    def test_extracts_concatenated_text(self) -> None:
        client = _client_returning(
            _response(
                content=[_text_block("alpha "), _text_block("beta")],
                stop_reason="end_turn",
            ),
        )
        loop = AgentLoop(client, [], "sys", "claude-sonnet-4-5-20250929")
        assert loop.run("hi").text == "alpha beta"


# ----------------------------------------------------------------- tool_use


class TestToolUse:
    def test_executes_tool_then_returns_text(self) -> None:
        client = _client_returning(
            _response(
                content=[_tool_use_block("echo", {"msg": "hi"}, tool_id="tu_a")],
                stop_reason="tool_use",
            ),
            _response(content=[_text_block("ok")], stop_reason="end_turn"),
        )
        recorded: list[dict[str, Any]] = []
        loop = AgentLoop(
            client,
            [_echo_tool(recorded)],
            "sys",
            "claude-sonnet-4-5-20250929",
        )
        result = loop.run("go")

        assert result.text == "ok"
        assert result.turns == 2
        assert recorded == [{"msg": "hi"}]

        # The second messages.create call must include the tool_result the
        # loop appended after executing the tool.
        sent_messages = client.messages_per_call[1]
        last_user = sent_messages[-1]
        assert last_user["role"] == "user"
        tool_results = last_user["content"]
        assert tool_results[0]["type"] == "tool_result"
        assert tool_results[0]["tool_use_id"] == "tu_a"
        assert tool_results[0]["content"] == "echo: hi"
        assert "is_error" not in tool_results[0]

    def test_tool_failure_returned_as_error_not_raised(self) -> None:
        client = _client_returning(
            _response(
                content=[_tool_use_block("boom", {}, tool_id="tu_x")],
                stop_reason="tool_use",
            ),
            _response(content=[_text_block("recovered")], stop_reason="end_turn"),
        )
        loop = AgentLoop(client, [_failing_tool()], "sys", "claude-sonnet-4-5-20250929")
        result = loop.run("go")

        assert result.text == "recovered"
        tool_results = client.messages_per_call[1][-1]["content"]
        assert tool_results[0]["is_error"] is True
        assert "boom" in tool_results[0]["content"]

    def test_unknown_tool_returns_error(self) -> None:
        client = _client_returning(
            _response(
                content=[_tool_use_block("does_not_exist", {}, tool_id="tu_z")],
                stop_reason="tool_use",
            ),
            _response(content=[_text_block("done")], stop_reason="end_turn"),
        )
        loop = AgentLoop(client, [], "sys", "claude-sonnet-4-5-20250929")
        result = loop.run("go")
        tool_results = client.messages_per_call[1][-1]["content"]
        assert tool_results[0]["is_error"] is True
        assert "Unknown tool" in tool_results[0]["content"]
        assert result.text == "done"


# ---------------------------------------------------------------- usage / max


class TestTokenUsage:
    def test_accumulates_across_turns(self) -> None:
        client = _client_returning(
            _response(
                content=[_tool_use_block("echo", {"msg": "x"})],
                stop_reason="tool_use",
                usage=_usage(input_tokens=100, output_tokens=20),
            ),
            _response(
                content=[_text_block("ok")],
                stop_reason="end_turn",
                usage=_usage(input_tokens=110, output_tokens=15),
            ),
        )
        loop = AgentLoop(client, [_echo_tool()], "sys", "claude-sonnet-4-5-20250929")
        result = loop.run("go")
        assert result.token_usage.input_tokens == 210
        assert result.token_usage.output_tokens == 35


class TestMaxTurns:
    def test_raises_when_exceeded(self) -> None:
        client = MagicMock()
        # Always return tool_use so the loop never naturally ends.
        client.messages.create.return_value = _response(
            content=[_tool_use_block("echo", {"msg": "loop"})],
            stop_reason="tool_use",
        )
        loop = AgentLoop(client, [_echo_tool()], "sys", "claude-sonnet-4-5-20250929")
        with pytest.raises(MaxTurnsExceeded):
            loop.run("go", max_turns=3)
        assert client.messages.create.call_count == 3


class TestUnexpectedStopReason:
    def test_raises(self) -> None:
        client = _client_returning(
            _response(content=[_text_block("partial")], stop_reason="max_tokens"),
        )
        loop = AgentLoop(client, [], "sys", "claude-sonnet-4-5-20250929")
        with pytest.raises(UnexpectedStopReason):
            loop.run("go")


# --------------------------------------------------------------- retries


def _make_status_error(cls: type[APIStatusError], status_code: int) -> APIStatusError:
    """Build a real Anthropic SDK error subclass instance.

    The SDK's APIStatusError requires a `response` and a `body`, but
    they're tolerant of duck-typed inputs in practice. We use a minimal
    SimpleNamespace; the loop only inspects str(exc).
    """
    response = SimpleNamespace(
        status_code=status_code,
        headers={},
        request=SimpleNamespace(method="POST", url="https://api.anthropic.com"),
    )
    return cls(message="boom", response=response, body=None)  # type: ignore[arg-type]


class TestRateLimitRetry:
    def test_retries_then_succeeds(self) -> None:
        client = MagicMock()
        success = _response(content=[_text_block("ok")], stop_reason="end_turn")
        client.messages.create.side_effect = [
            _make_status_error(RateLimitError, 429),
            _make_status_error(RateLimitError, 429),
            success,
        ]
        sleeps: list[float] = []
        loop = AgentLoop(
            client,
            [],
            "sys",
            "claude-sonnet-4-5-20250929",
            max_retries=3,
            sleep=sleeps.append,
        )
        result = loop.run("go")
        assert result.text == "ok"
        # Exponential backoff: base * 2^0, base * 2^1
        assert sleeps == [1.0, 2.0]
        assert client.messages.create.call_count == 3

    def test_raises_after_exhausting_retries(self) -> None:
        client = MagicMock()
        client.messages.create.side_effect = [
            _make_status_error(RateLimitError, 429),
            _make_status_error(RateLimitError, 429),
            _make_status_error(RateLimitError, 429),
            _make_status_error(RateLimitError, 429),
        ]
        loop = AgentLoop(
            client,
            [],
            "sys",
            "claude-sonnet-4-5-20250929",
            max_retries=3,
            sleep=lambda _s: None,
        )
        with pytest.raises(RateLimitExhausted):
            loop.run("go")


class TestBadRequest:
    def test_does_not_retry_and_raises_invalid_request(self) -> None:
        client = MagicMock()
        client.messages.create.side_effect = _make_status_error(BadRequestError, 400)
        loop = AgentLoop(client, [], "sys", "claude-sonnet-4-5-20250929")
        with pytest.raises(InvalidRequestError):
            loop.run("go")
        assert client.messages.create.call_count == 1


class TestOtherApiError:
    def test_raises_agent_error(self) -> None:
        from anthropic import InternalServerError

        client = MagicMock()
        client.messages.create.side_effect = _make_status_error(InternalServerError, 500)
        loop = AgentLoop(client, [], "sys", "claude-sonnet-4-5-20250929")
        with pytest.raises(AgentError):
            loop.run("go")


# --------------------------------------------------------------- repository


class _RecordingRepo:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str, str, str]] = []

    def append_log(self, run_id: str, level: str, source: str, message: str) -> None:
        self.logs.append((run_id, level, source, message))


class TestRepositoryLogging:
    def test_logs_tool_call_and_failure(self) -> None:
        client = _client_returning(
            _response(
                content=[_tool_use_block("boom", {"x": 1}, tool_id="tu_q")],
                stop_reason="tool_use",
            ),
            _response(content=[_text_block("done")], stop_reason="end_turn"),
        )
        repo = _RecordingRepo()
        loop = AgentLoop(
            client,
            [_failing_tool()],
            "sys",
            "claude-sonnet-4-5-20250929",
            repository=repo,  # type: ignore[arg-type]
            run_id="run-123",
        )
        loop.run("go")
        # Two log lines: one for the call, one for the failure.
        assert len(repo.logs) == 2
        run_id, level, source, message = repo.logs[0]
        assert run_id == "run-123"
        assert level == "info"
        assert source == "agent"
        assert "Calling tool: boom" in message
        assert '"x": 1' in message
        assert repo.logs[1][1] == "warning"
        assert "boom" in repo.logs[1][3]

    def test_no_logging_without_repository(self) -> None:
        # Without repo + run_id, the loop should still succeed.
        client = _client_returning(
            _response(content=[_text_block("ok")], stop_reason="end_turn"),
        )
        loop = AgentLoop(client, [], "sys", "claude-sonnet-4-5-20250929")
        assert loop.run("go").text == "ok"


# --------------------------------------------------------------- token usage


class TestTokenUsageDataclass:
    def test_add_handles_missing_cache_fields(self) -> None:
        usage = TokenUsage()
        # Mimic an SDK response with no cache fields.
        usage.add(SimpleNamespace(input_tokens=3, output_tokens=2))
        assert usage.input_tokens == 3
        assert usage.output_tokens == 2
        assert usage.cache_creation_tokens == 0
        assert usage.cache_read_tokens == 0

    def test_cost_for_known_model(self) -> None:
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        cost = usage.cost_usd("claude-sonnet-4-5")
        # 1M input @ $3 + 1M output @ $15 = $18
        assert cost == pytest.approx(18.0)

    def test_cost_for_unknown_model_is_zero(self) -> None:
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        assert usage.cost_usd("nonexistent-model") == 0.0

    def test_cost_for_dated_model_resolves(self) -> None:
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=0)
        # Dated snapshot id should resolve to the base pricing.
        assert usage.cost_usd("claude-sonnet-4-5-20250929") == pytest.approx(3.0)


# --------------------------------------------------------------- prompt


class TestSystemPrompt:
    def test_prompt_loads(self, tmp_path: Path) -> None:
        prompt = load_m1_code_agent_prompt()
        assert "Carve" in prompt
        assert "read_file" in prompt
        assert "write_file" in prompt
        assert "run_snowflake_query" in prompt
