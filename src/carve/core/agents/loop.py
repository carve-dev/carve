"""Generic Anthropic tool-use loop.

`AgentLoop` is the engine every Carve agent uses. M1 wires it up with
the three hardcoded tools in `m1_tools.py` and the prompt at
`prompts/m1_code_agent.md`. M2 will reuse it for specialist agents
with different tools and prompts — the loop knows nothing about what
its tools do.

Sync, not async (per spec): the user-facing CLI is sync, tool work is
naturally sync, and the M2 API server will wrap the whole thing in a
threadpool. Don't pull asyncio into this file.

Persistence is optional. If a `Repository` and `run_id` are provided
the loop appends an `info` log line per tool call and a structured
`info` line on completion with token totals. Cost is not stored on the
run row by the loop itself — the caller (CLI) writes it on completion
because the loop has no opinion about run lifecycle.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from anthropic import APIStatusError, BadRequestError, RateLimitError

from carve.core.agents.exceptions import (
    AgentError,
    InvalidRequestError,
    MaxTurnsExceeded,
    RateLimitExhausted,
    UnexpectedStopReason,
)
from carve.core.agents.observer import AgentObserver, NullObserver
from carve.core.agents.pricing import compute_cost_usd

if TYPE_CHECKING:
    from carve.core.agents.tools import Tool
    from carve.core.state.repository import Repository

logger = logging.getLogger(__name__)

# Where the M1 system prompt lives, relative to this file. Loaded lazily so
# importing the module never touches disk.
_PROMPTS_DIR = Path(__file__).parent / "prompts"


class _MessagesAPI(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class _AnthropicLike(Protocol):
    """The slice of the Anthropic client the loop actually uses.

    Declaring it as a protocol means tests can pass a `MagicMock`
    without `cast`s, and we only depend on `client.messages.create`.
    """

    @property
    def messages(self) -> _MessagesAPI: ...


@dataclass
class TokenUsage:
    """Cumulative token counters across all turns of a single run.

    Fields mirror the Anthropic `Usage` object. Cache fields are
    optional in the SDK's response, so `add()` defaults them to 0.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, usage: Any) -> None:
        """Accumulate a single response's `usage` block."""
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_creation_tokens += int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        self.cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)

    def cost_usd(self, model: str) -> float:
        """Compute USD cost for accumulated usage on `model`."""
        return compute_cost_usd(
            model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens,
        )


@dataclass
class AgentResult:
    """Outcome of `AgentLoop.run`."""

    text: str
    token_usage: TokenUsage
    turns: int
    messages: list[dict[str, Any]] = field(default_factory=list)


def load_m1_code_agent_prompt() -> str:
    """Load the M1 code-agent system prompt from disk."""
    return (_PROMPTS_DIR / "m1_code_agent.md").read_text(encoding="utf-8")


class AgentLoop:
    """Tool-use turn-taking loop over the Anthropic Messages API.

    The loop is single-conversation: instantiate one `AgentLoop` per
    `run()` call. Reusing across goals would mean carrying stale
    `messages` and `token_usage` across runs, which is rarely what
    callers want.
    """

    def __init__(
        self,
        client: _AnthropicLike,
        tools: list[Tool],
        system_prompt: str,
        model: str,
        *,
        repository: Repository | None = None,
        run_id: str | None = None,
        max_tokens: int = 4096,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        sleep: Any = time.sleep,
        observer: AgentObserver | None = None,
    ) -> None:
        self.client = client
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.system_prompt = system_prompt
        self.model = model
        self.repository = repository
        self.run_id = run_id
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._sleep = sleep
        self.observer: AgentObserver = observer if observer is not None else NullObserver()
        self.messages: list[dict[str, Any]] = []
        self.token_usage = TokenUsage()
        self._tool_calls_total = 0

    # ------------------------------------------------------------------ run

    def run(self, user_message: str, max_turns: int = 30) -> AgentResult:
        """Drive the conversation until the model ends its turn."""
        self.messages.append({"role": "user", "content": user_message})

        for turn in range(1, max_turns + 1):
            self.observer.on_turn_start(turn)
            response = self._call_api()
            self.token_usage.add(response.usage)
            self.observer.on_turn_complete(
                turn,
                self.token_usage.input_tokens,
                self.token_usage.output_tokens,
            )
            assistant_content = self._content_to_serializable(response.content)
            self.messages.append({"role": "assistant", "content": assistant_content})

            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "end_turn":
                self.observer.on_done(
                    total_turns=turn,
                    total_tool_calls=self._tool_calls_total,
                    input_tokens=self.token_usage.input_tokens,
                    output_tokens=self.token_usage.output_tokens,
                    cost_usd=self.token_usage.cost_usd(self.model),
                )
                return AgentResult(
                    text=self._extract_text(response),
                    token_usage=self.token_usage,
                    turns=turn,
                    messages=list(self.messages),
                )
            if stop_reason == "tool_use":
                tool_results = self._execute_tool_calls(response)
                self.messages.append({"role": "user", "content": tool_results})
                continue

            raise UnexpectedStopReason(
                f"Unexpected stop_reason from Anthropic: {stop_reason!r}"
            )

        raise MaxTurnsExceeded(f"Agent exceeded max turns ({max_turns})")

    # ---------------------------------------------------------------- API

    def _call_api(self) -> Any:
        """Call `messages.create` with retries on rate-limit errors.

        Exponential backoff, capped at `max_retries`. Non-retryable
        4xx errors raise `InvalidRequestError` immediately.
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.client.messages.create(
                    model=self.model,
                    system=self.system_prompt,
                    max_tokens=self.max_tokens,
                    tools=[t.to_schema() for t in self.tools.values()],
                    messages=self.messages,
                )
            except RateLimitError as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                delay = self.retry_base_delay * (2**attempt)
                logger.warning(
                    "Anthropic rate-limited (attempt %d/%d); sleeping %.1fs",
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                )
                self._sleep(delay)
            except BadRequestError as exc:
                raise InvalidRequestError(f"Anthropic rejected the request: {exc}") from exc
            except APIStatusError as exc:
                # Non-rate-limit 4xx/5xx — surface as agent error.
                raise AgentError(f"Anthropic API error: {exc}") from exc

        raise RateLimitExhausted(
            f"Anthropic rate limit not cleared after {self.max_retries} retries"
        ) from last_exc

    # ---------------------------------------------------------------- tools

    def _execute_tool_calls(self, response: Any) -> list[dict[str, Any]]:
        """Run every tool_use block in `response.content` in order.

        Returns the list of tool_result blocks the loop appends as the
        next user message. Failures are caught and returned as
        ``is_error=True`` results so the model can try again.
        """
        results: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_name = getattr(block, "name", "")
            tool_input = getattr(block, "input", {}) or {}
            tool_use_id = getattr(block, "id", "")
            self._log_tool_call(tool_name, tool_input)
            self.observer.on_tool_call(tool_name, dict(tool_input))
            self._tool_calls_total += 1

            tool = self.tools.get(tool_name)
            if tool is None:
                msg = f"Unknown tool: {tool_name!r}"
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": msg,
                        "is_error": True,
                    }
                )
                self.observer.on_tool_result(
                    tool_name, ok=False, summary=msg, duration_ms=0
                )
                continue

            start = time.perf_counter_ns()
            try:
                output = tool.executor(dict(tool_input))
            except Exception as exc:
                duration_ms = (time.perf_counter_ns() - start) // 1_000_000
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": str(exc),
                        "is_error": True,
                    }
                )
                self._log_tool_error(tool_name, exc)
                self.observer.on_tool_result(
                    tool_name,
                    ok=False,
                    summary=str(exc),
                    duration_ms=int(duration_ms),
                )
                continue

            duration_ms = (time.perf_counter_ns() - start) // 1_000_000
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": _stringify_tool_output(output),
                }
            )
            self.observer.on_tool_result(
                tool_name,
                ok=True,
                summary=_summarize_tool_result(tool_name, output),
                duration_ms=int(duration_ms),
            )
        return results

    # ---------------------------------------------------------------- log

    def _log_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        if self.repository is None or self.run_id is None:
            return
        try:
            payload = json.dumps(tool_input, default=str)
        except (TypeError, ValueError):
            payload = repr(tool_input)
        self.repository.append_log(
            run_id=self.run_id,
            level="info",
            source="agent",
            message=f"Calling tool: {tool_name} with input: {payload}",
        )

    def _log_tool_error(self, tool_name: str, exc: Exception) -> None:
        if self.repository is None or self.run_id is None:
            return
        self.repository.append_log(
            run_id=self.run_id,
            level="warning",
            source="agent",
            message=f"Tool {tool_name} failed: {exc}",
        )

    # ---------------------------------------------------------------- text

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Concatenate all text blocks from the assistant message."""
        parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", ""))
        return "".join(parts)

    @staticmethod
    def _content_to_serializable(content: Any) -> list[dict[str, Any]]:
        """Convert SDK content blocks to plain dicts for the messages list.

        The SDK accepts either pydantic model instances or plain dicts
        when echoing assistant content back. Plain dicts make the
        messages list trivially serializable for tests and logging.
        """
        out: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, dict):
                out.append(block)
                continue
            block_type = getattr(block, "type", None)
            if block_type == "text":
                out.append({"type": "text", "text": getattr(block, "text", "")})
            elif block_type == "tool_use":
                out.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", {}) or {},
                    }
                )
            else:
                # Future-proof: best-effort dump via model_dump if present.
                dump = getattr(block, "model_dump", None)
                out.append(dump() if callable(dump) else {"type": str(block_type)})
        return out


def _stringify_tool_output(output: Any) -> str:
    """Render a tool's return value as a string for the tool_result block."""
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, default=str)
    except (TypeError, ValueError):
        return repr(output)


def _summarize_tool_result(name: str, result: Any) -> str:
    """Build a short caller-friendly summary of `result` for the observer.

    Tools return either a string (e.g. `read_file` content) or a dict
    (e.g. `write_file` returns ``{"path": ..., "bytes_written": ...}``,
    `run_snowflake_query` returns ``{"row_count": ..., "rows": [...]}``).
    The summary picks an obvious field per tool, with `"ok"` as the
    fall-through. This is observer-only — the loop still serializes
    the full result into the tool_result block separately.
    """
    if isinstance(result, dict):
        if "row_count" in result:
            try:
                return f"{int(result['row_count'])} rows"
            except (TypeError, ValueError):
                return "ok"
        if "bytes_written" in result:
            return _format_bytes(result["bytes_written"]) + " written"
        if "rows" in result and isinstance(result["rows"], list):
            return f"{len(result['rows'])} rows"
        return "ok"
    if isinstance(result, str):
        # `read_file` returns the file contents directly.
        size = len(result.encode("utf-8"))
        return f"{_format_bytes(size)} read"
    if isinstance(result, list):
        return f"{len(result)} items"
    return "ok"


def _format_bytes(n: Any) -> str:
    """Render a byte count compactly: ``31 B``, ``1.8 KB``, ``2.4 MB``."""
    try:
        value = int(n)
    except (TypeError, ValueError):
        return "0 B"
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"
