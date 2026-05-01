# M1.1-04 — Live progress output during `carve plan`

**Milestone:** 1.1 — M1 follow-ups
**Estimated effort:** 0.25 day
**Dependencies:** M1-04 (agent loop), M1 integration (orchestrator/planner)

## Purpose

Today, `carve plan "<goal>"` runs the agent loop synchronously and prints nothing until the loop returns — typically 30 seconds to 2+ minutes later. Users see a frozen terminal and reasonably suspect the CLI is broken. Surfaced during the first real M1 smoke test.

Make `carve plan` chatty by default: print each tool call as it happens, and a final cumulative-cost line. The mechanics are simple — `AgentLoop` already knows about every turn — what's missing is a hook that lets the planner observe them in real time.

## Scope

### In scope

- A small `Observer` callback interface on `AgentLoop` that fires on key events: turn start, tool call (with name + brief args), tool result (success/failure), turn complete (with running token counts), final completion.
- A default `RichConsoleObserver` implementation used by `carve plan`. Output looks like:

  ```
  ⠋ Planning · turn 1 · 0 tools called · 0 tokens
  → read_file(path="carve.toml")
  ✓ ok (12ms)
  → run_snowflake_query(sql="SHOW SCHEMAS")
  ✓ 14 rows (320ms)
  → write_file(path="pipelines/iowa_liquor/main.py")
  ✓ 1.8 KB written
  → write_file(path="pipelines/iowa_liquor/requirements.txt")
  ✓ 31 B written
  ✓ Plan generated: plan_20260430_124501_a1b2c3
    (5 tool calls · 18,432 tokens in / 4,210 out · $0.07)
  ```

  Use `rich.live.Live` for the spinner line; the per-call `→`/`✓` lines are plain `console.print`.

- A `--quiet` typer flag on `carve plan` that suppresses everything except the final summary (for CI / scripted use).
- Truncate tool args to keep lines from wrapping. `path="..."` shows the basename; `sql="..."` shows the first ~60 chars; `content="..."` is omitted entirely (way too long, never useful in a one-line trace).
- Distinguish tool failures clearly: `✗ <error msg>` in red, but the loop keeps going (M1-04 already returns errors as `is_error=True` tool_results, the loop doesn't crash).

### Out of scope

- Mid-stream model output (the agent's thinking text mid-turn). The Anthropic SDK supports streaming `messages.stream(...)`, but switching to streaming for `carve plan` is a bigger change than the observability gap warrants.
- Progress for `carve apply` — the apply path already streams subprocess logs via the live-tail loop; it's the plan path that's silent.
- Persisting tool-call traces. M1-04 already does that when `run_id` is set; here we just want stdout.
- Redacting sensitive values in tool args. The arg-truncation rules above incidentally avoid the worst leaks (no `content` printed, sql truncated), but a real redaction policy belongs with the agent loop's logging in a later spec.

## Implementation

### Observer interface

`src/carve/core/agents/observer.py`:

```python
from typing import Protocol


class AgentObserver(Protocol):
    def on_turn_start(self, turn: int) -> None: ...
    def on_tool_call(self, name: str, input: dict) -> None: ...
    def on_tool_result(self, name: str, ok: bool, summary: str, duration_ms: int) -> None: ...
    def on_turn_complete(self, turn: int, input_tokens: int, output_tokens: int) -> None: ...
    def on_done(self, total_turns: int, total_tool_calls: int, input_tokens: int, output_tokens: int, cost_usd: float) -> None: ...


class NullObserver:
    """No-op observer. Default when the planner doesn't pass one."""
    def on_turn_start(self, turn: int) -> None: pass
    def on_tool_call(self, name: str, input: dict) -> None: pass
    def on_tool_result(self, name: str, ok: bool, summary: str, duration_ms: int) -> None: pass
    def on_turn_complete(self, turn: int, input_tokens: int, output_tokens: int) -> None: pass
    def on_done(self, total_turns: int, total_tool_calls: int, input_tokens: int, output_tokens: int, cost_usd: float) -> None: pass
```

Backward-compatible: `AgentLoop.__init__` gains an optional `observer: AgentObserver = NullObserver()`. Existing callers and tests continue to work without changes.

### `AgentLoop` wiring

In `loop.py`'s `run()` method:

- At loop top: `observer.on_turn_start(turn)`.
- Before invoking each tool executor: `observer.on_tool_call(name, input)`. Wrap the executor call in a `time.perf_counter_ns()` measurement.
- After each tool returns (or raises): `observer.on_tool_result(name, ok, summary, duration_ms)`. The `summary` is a short caller-friendly string the executor returns, e.g. `"14 rows"`, `"1.8 KB written"`, or the error string when `ok=False`. Compute `summary` from the tool's return value: tools return `dict`, so we extract a sensible field (rows count for query, byte count for write, content length for read, etc.). When unsure, fall back to `"ok"`.
- After each `messages.create` call returns and `token_usage` is updated: `observer.on_turn_complete(turn, total_input, total_output)`.
- On `end_turn`: `observer.on_done(...)` with cumulative numbers.

The summary-extraction helper lives in `loop.py` as a small private function; tools don't need to know about observers.

### `RichConsoleObserver`

> **Updated during implementation (2026-04-29):** added a public idempotent `close()` method, a non-TTY short-circuit in `_ensure_live`, and `rich.markup.escape` on tool name + arg values to prevent model-controlled strings from injecting Rich markup.

`src/carve/cli/orchestrator/observers.py`:

- Constructor takes a `rich.console.Console` and a `quiet: bool` flag.
- Maintains a `rich.live.Live` instance for the spinner status line, started in `on_turn_start(1)` and stopped in `on_done(...)`.
- Exposes a public `close()` method that idempotently stops the `Live` region. The CLI calls it from a `finally` block around `generate_plan(...)` so the cursor is restored even when the agent loop raises (e.g. `MaxTurnsExceeded`, `RateLimitExhausted`).
- `_ensure_live` short-circuits when `console.is_terminal` is False — non-TTY stdout (CI, piped output) gets the per-event lines but no cursor-control sequences.
- `on_tool_call`: stops live for the duration, prints the `→ name(args)` line, restarts live (or just uses `live.console.log(...)` which prints above the live region).
- Truncation rules:
  - `path` argument → `basename(path)` only.
  - `sql` argument → first 60 chars + `…` if longer.
  - Any string argument over 80 chars → first 60 chars + `…`.
  - `content` arg → omit entirely.
  - All other args → repr'd at most 40 chars.
- Tool name and every argument value are passed through `rich.markup.escape` before being assembled into the `→`/`✓` lines, since those lines are printed with `markup=True`.
- In `quiet=True` mode, only `on_done` produces output.

### Planner wiring

`src/carve/cli/orchestrator/planner.py`:

```python
def generate_plan(..., observer: AgentObserver | None = None) -> PlanArtifact:
    ...
    observer = observer or NullObserver()
    loop = AgentLoop(..., observer=observer)
    ...
```

`src/carve/cli/commands/plan.py`:

```python
@app.command()
def command(goal: str, quiet: bool = typer.Option(False, "--quiet", "-q")):
    ...
    observer = RichConsoleObserver(console, quiet=quiet)
    try:
        artifact = generate_plan(..., observer=observer)
    finally:
        observer.close()
    ...
```

> **Updated during implementation (2026-04-29):** the `generate_plan(...)` call is wrapped in `try`/`finally` that calls `observer.close()`. Without it, when the agent loop raises (e.g. `MaxTurnsExceeded`, `RateLimitExhausted`), the `Live` region never tears down and the terminal cursor stays hidden.

The existing per-plan summary line stays; the observer just adds the running play-by-play above it.

## Tests

`tests/core/agents/test_loop.py` — extend:

- `test_observer_receives_turn_and_tool_events` — mocked client returns one tool_use turn then end_turn; assert observer recorded `on_turn_start(1)`, `on_tool_call`, `on_tool_result`, `on_turn_complete`, `on_done` in order, with the right counts.
- `test_observer_records_tool_failure` — tool executor raises; assert `on_tool_result(name, ok=False, ...)` was called and the loop kept going.
- `test_observer_default_is_null_observer` — no observer passed, no errors, nothing printed.

`tests/cli/orchestrator/test_observers.py` (new):

- `test_rich_console_observer_prints_tool_calls` — capture `Console` output, assert each line shape.
- `test_rich_console_observer_truncates_long_args` — sql > 60 chars renders with `…`.
- `test_rich_console_observer_omits_content_arg` — write_file with a 10 KB content arg renders without the body.
- `test_quiet_mode_only_prints_summary` — observer with `quiet=True` produces no output until `on_done`.

`tests/cli/orchestrator/test_planner.py` — update the existing happy-path test to pass an observer mock and assert the planner forwarded events through. Existing assertions about plan persistence and summary keep passing.

## Acceptance criteria

- During `carve plan "<goal>"`, the user sees a tool call printed within ~1 second of each one happening.
- Final summary still includes plan id, pipeline path, requirements, token cost.
- `--quiet` produces output identical to today's behavior (final summary only).
- No regression in test runtime — observer overhead is negligible.
- `ruff` + `mypy --strict` + full `pytest` stay green.
- Short `## [Unreleased]` note in `CHANGELOG.md`.

## Files this spec produces

> **Updated during implementation (2026-04-29):** added `src/carve/py.typed` (marker so `mypy --strict tests/` resolves carve imports — needed to drop now-unused `# type: ignore[arg-type]` comments) and a related cleanup edit in `tests/cli/orchestrator/test_applier.py`.

New:

- `src/carve/core/agents/observer.py`
- `src/carve/cli/orchestrator/observers.py`
- `tests/cli/orchestrator/test_observers.py`
- `src/carve/py.typed` (PEP 561 marker; lets downstream type-checkers resolve `carve.*` imports)

Modified:

- `src/carve/core/agents/loop.py` (observer hooks + summary extraction)
- `src/carve/core/agents/__init__.py` (export observer types)
- `src/carve/cli/orchestrator/planner.py` (accept observer arg)
- `src/carve/cli/commands/plan.py` (--quiet flag, instantiate observer, `try`/`finally` around `generate_plan` to call `observer.close()`)
- `tests/core/agents/test_loop.py` (observer tests)
- `tests/cli/orchestrator/test_planner.py` (observer wiring assertion)
- `tests/cli/orchestrator/test_applier.py` (drop unused `# type: ignore[no-any-return]`, now redundant with `py.typed`)
- `CHANGELOG.md`

## What this enables

- Carve stops looking broken on the first run.
- A future "agent debug mode" can ship a richer observer (full args, full results, JSON logging) without touching `AgentLoop`.
- M2's WebSocket layer can implement a `WebSocketObserver` and stream the same events to a browser. Same protocol, different sink.
