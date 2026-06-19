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
    from collections.abc import Callable

    from carve.core.agents.cancel import CancellationToken
    from carve.core.agents.permissions.gate import Approver, PermissionGate
    from carve.core.agents.steering import SteeringQueue
    from carve.core.agents.tools import Tool
    from carve.core.skills.context import SkillContext
    from carve.core.skills.executor import CachedSkillExecutor
    from carve.core.skills.registry import SkillRegistry
    from carve.core.state.repository import Repository

    # Hook fire-points (the declarative `hooks.toml` format is spec 16;
    # the harness ships only these call sites). A hook receives the tool
    # name + input and may raise to abort the call.
    Hook = Callable[[str, dict[str, Any]], None]

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


def load_m1_plan_agent_prompt() -> str:
    """Load the M1 plan-agent system prompt from disk."""
    return (_PROMPTS_DIR / "m1_plan_agent.md").read_text(encoding="utf-8")


def load_m1_build_agent_prompt() -> str:
    """Load the M1 build-agent system prompt from disk."""
    return (_PROMPTS_DIR / "m1_build_agent.md").read_text(encoding="utf-8")


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
        terminator_tool: str | None = None,
        skills: SkillRegistry | None = None,
        skill_executor: CachedSkillExecutor | None = None,
        skill_context: SkillContext | None = None,
        gate: PermissionGate | None = None,
        approver: Approver | None = None,
        cancellation: CancellationToken | None = None,
        steering: SteeringQueue | None = None,
        pre_tool_hook: Hook | None = None,
        post_tool_hook: Hook | None = None,
    ) -> None:
        self.client = client
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        # Skill registry integration: when supplied, the loop exposes
        # each registered skill to the model as a tool and dispatches
        # invocations through the (optional) `CachedSkillExecutor`. The
        # skill names must not collide with regular tool names — this
        # keeps the dispatch table unambiguous.
        self.skills = skills
        if skills is not None:
            for name in skills.names():
                if name in self.tools:
                    raise ValueError(
                        f"Skill name {name!r} collides with an existing tool. "
                        "Skill and tool names share a single namespace."
                    )
            if skill_context is None:
                raise ValueError(
                    "skill_context is required when skills are passed to AgentLoop."
                )
        self.skill_executor = skill_executor
        self.skill_context = skill_context
        self.system_prompt = system_prompt
        self.model = model
        self.repository = repository
        self.run_id = run_id
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._sleep = sleep
        self.observer: AgentObserver = observer if observer is not None else NullObserver()
        # When set, the loop exits immediately after a turn in which any
        # tool call's name matches this string. The plan agent uses this
        # to make `submit_plan` an authoritative terminator: a misbehaving
        # model can't call `submit_plan` and then keep working, and a
        # second invocation (rejected by the executor) never overwrites
        # the captured design.
        self.terminator_tool = terminator_tool
        # The pre-execution permission gate. When set, the loop calls it
        # *before* dispatching any tool (the authoritative boundary); a
        # `deny` becomes an `is_error` tool_result and a
        # `needs_user_input` becomes a surfaced no-execution outcome —
        # neither reaches the tool executor. When None (legacy callers /
        # M1) every tool runs as before.
        self.gate = gate
        self.approver = approver
        # Cancellation + steering are checked *between turns* only, so an
        # in-flight turn always completes cleanly. Both are no-ops when
        # unset (the batch paths pass neither).
        self.cancellation = cancellation
        self.steering = steering
        # Hook fire-points (spec 16 plugs the declarative format in here);
        # the harness ships only the call sites. A hook may raise to abort.
        self.pre_tool_hook = pre_tool_hook
        self.post_tool_hook = post_tool_hook
        self.messages: list[dict[str, Any]] = []
        self.token_usage = TokenUsage()
        self._tool_calls_total = 0
        # Harness-tracked log of files mutated by `edit`/`create_file`
        # this run. The loop appends here on a *successful* write; the
        # SubagentRunner reads it into DelegationResult.files_changed.
        # The model never self-reports this — its terminator carries
        # `outputs`, not the file list.
        self.files_changed: list[str] = []

    # ------------------------------------------------------------------ run

    def run(self, user_message: str, max_turns: int = 30) -> AgentResult:
        """Drive the conversation until the model ends its turn."""
        self.messages.append({"role": "user", "content": user_message})

        for turn in range(1, max_turns + 1):
            # Between-turns checks (and before the first turn): a tripped
            # cancellation token stops the loop cleanly; queued steering
            # guidance is appended as a user message before the next API
            # call. Both are no-ops when their handle is unset, so the
            # batch paths are unaffected.
            if self.cancellation is not None:
                self.cancellation.raise_if_cancelled()
            if self.steering is not None:
                for guidance in self.steering.drain():
                    self.messages.append({"role": "user", "content": guidance})

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
                if self._terminator_invoked(response):
                    # The terminator tool fired this turn — exit the loop
                    # without making another `messages.create` call. The
                    # model didn't get to summarize; an empty text result
                    # is the correct synthetic response.
                    self.observer.on_done(
                        total_turns=turn,
                        total_tool_calls=self._tool_calls_total,
                        input_tokens=self.token_usage.input_tokens,
                        output_tokens=self.token_usage.output_tokens,
                        cost_usd=self.token_usage.cost_usd(self.model),
                    )
                    return AgentResult(
                        text="",
                        token_usage=self.token_usage,
                        turns=turn,
                        messages=list(self.messages),
                    )
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
                tool_schemas: list[dict[str, Any]] = [
                    t.to_schema() for t in self.tools.values()
                ]
                if self.skills is not None:
                    tool_schemas.extend(self.skills.to_tool_schemas())
                return self.client.messages.create(
                    model=self.model,
                    system=self.system_prompt,
                    max_tokens=self.max_tokens,
                    tools=tool_schemas,
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
            is_skill = (
                tool is None
                and self.skills is not None
                and tool_name in self.skills
            )
            if tool is None and not is_skill:
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

            # --- Gate first: the authoritative pre-execution boundary. ---
            # Order is gate → pre_tool hook → execute → post_tool hook. A
            # `deny` returns an is_error tool_result and a
            # `needs_user_input` returns a surfaced no-execution outcome;
            # in both cases the tool/skill executor is NOT called. When no
            # gate is wired (M1/legacy), every call proceeds as before.
            if self.gate is not None:
                decision = self.gate.check(
                    tool_name, dict(tool_input), approver=self.approver
                )
                if not decision.allowed:
                    results.append(
                        self._gate_blocked_result(tool_use_id, decision)
                    )
                    self.observer.on_tool_result(
                        tool_name,
                        ok=False,
                        summary=f"{decision.outcome.value}: {decision.reason}",
                        duration_ms=0,
                    )
                    continue

            start = time.perf_counter_ns()
            try:
                if self.pre_tool_hook is not None:
                    self.pre_tool_hook(tool_name, dict(tool_input))
                if is_skill:
                    output = self._execute_skill(tool_name, dict(tool_input))
                else:
                    assert tool is not None  # narrowed by `is_skill` branch above
                    output = tool.executor(dict(tool_input))
                if self.post_tool_hook is not None:
                    self.post_tool_hook(tool_name, dict(tool_input))
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
            # Harness-tracked files_changed: a *successful* edit/create_file
            # returns {"path": ...}; record it from the tool's own output,
            # never from a model self-report.
            self._track_file_change(tool_name, output)
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

    @staticmethod
    def _gate_blocked_result(tool_use_id: str, decision: Any) -> dict[str, Any]:
        """Build the tool_result for a gate `deny` / `needs_user_input`.

        Both are returned to the model as an ``is_error`` result so it can
        adapt (pick a different tool, narrow a path) or surface the
        approval need — the executor never ran.
        """
        prefix = (
            "Permission denied"
            if decision.outcome.value == "deny"
            else "Needs user approval"
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": f"{prefix}: {decision.reason}",
            "is_error": True,
        }

    def _track_file_change(self, tool_name: str, output: Any) -> None:
        """Append to ``files_changed`` when a write tool succeeded.

        Only ``edit`` / ``create_file`` (and the legacy ``write_file``)
        produce a tracked change. The path is read from the tool's
        structured output, so the log reflects what was actually written.
        """
        if tool_name not in ("edit", "create_file", "write_file"):
            return
        if isinstance(output, dict):
            path = output.get("path")
            if isinstance(path, str) and path not in self.files_changed:
                self.files_changed.append(path)

    def _execute_skill(self, name: str, kwargs: dict[str, Any]) -> Any:
        """Run skill `name` via `skill_executor` (or registry directly).

        The executor handles caching; absent one we fall back to a direct
        registry lookup for callers that don't need invocation-scoped
        caching (rare — production always passes one).
        """
        assert self.skills is not None
        assert self.skill_context is not None
        if self.skill_executor is not None:
            result = self.skill_executor.execute(name, kwargs, self.skill_context)
        else:
            fn = self.skills[name]
            result = fn(self.skill_context, **kwargs)
        # Convert SkillResult to a JSON-friendly dict for the tool_result.
        return {
            "data": result.data,
            "truncated": result.truncated,
            "total_count": result.total_count,
            **(
                {"next_cursor": result.next_cursor}
                if result.next_cursor is not None
                else {}
            ),
        }

    def _terminator_invoked(self, response: Any) -> bool:
        """Return True if any tool_use block in `response` matches the terminator."""
        if self.terminator_tool is None:
            return False
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            if getattr(block, "name", "") == self.terminator_tool:
                return True
        return False

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

    Tools return either a string (e.g. `read_file` content) or a dict.
    Tool dicts come in two shapes:

    1. Plain tool output: ``{"row_count": ..., "rows": [...]}`` (from
       ``run_snowflake_query``), ``{"bytes_written": ...}`` (from
       ``write_file``).
    2. Skill envelopes: ``{"data": {<kind>: [...]}, "truncated": bool,
       "total_count": int|None}`` (from any catalog skill — see
       ``_execute_skill``).

    The summary picks an obvious field per shape, with ``"ok"`` as the
    fall-through. This is observer-only — the loop still serializes
    the full result into the tool_result block separately.
    """
    if isinstance(result, dict):
        # Skill envelope: {"data": {...}, "truncated": ..., ...}
        if "data" in result and "truncated" in result:
            return _summarize_skill_envelope(result)
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


def _summarize_skill_envelope(envelope: dict[str, Any]) -> str:
    """Render ``{"data": {"tables": [...]}, "truncated": True, "total_count": 250}``
    as ``"200 of 250 tables (truncated)"`` (or ``"3 databases"`` when not
    truncated, or ``"exists: true"`` for boolean payloads).
    """
    data = envelope.get("data")
    truncated = bool(envelope.get("truncated"))
    total = envelope.get("total_count")
    if isinstance(data, dict):
        # Catalog skills wrap their list payload under a single key
        # (``tables`` / ``schemas`` / ``databases`` / ``columns``); the
        # boolean ``table_exists`` skill uses ``exists`` instead.
        for key, value in data.items():
            if isinstance(value, list):
                count = len(value)
                if truncated and isinstance(total, int):
                    return f"{count} of {total} {key} (truncated)"
                return f"{count} {key}"
            if isinstance(value, bool):
                return f"{key}: {'true' if value else 'false'}"
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
