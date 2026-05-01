"""Console-rendering `AgentObserver` implementations for the CLI.

`RichConsoleObserver` is the default observer wired into ``carve plan``.
It maintains a `rich.live.Live` spinner status line that updates as
turns/tool-calls accumulate, and prints `→ name(args)` and `✓ summary`
lines above the live region as each tool fires. Tool failures render
in red as `✗ <error>` but do not interrupt the loop.

A `--quiet` mode suppresses everything until `on_done`, restoring the
silent behaviour CI/scripted use cases want.

Truncation rules (kept tight to keep lines from wrapping):

* ``path`` argument → ``basename(path)``.
* ``sql`` argument → first 60 chars + ``…`` if longer.
* Any other string > 80 chars → first 60 chars + ``…``.
* ``content`` argument → omitted entirely (always too long, never useful).
* Other arg types → ``repr`` capped at 40 chars.
"""

from __future__ import annotations

import os
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.spinner import Spinner
from rich.text import Text

from carve.core.agents.observer import AgentObserver

# Truncation thresholds — see module docstring.
_SQL_KEEP = 60
_GENERIC_LIMIT = 80
_GENERIC_KEEP = 60
_REPR_CAP = 40
_ELLIPSIS = "…"


class RichConsoleObserver(AgentObserver):
    """Render agent progress to a `rich.console.Console`.

    Args:
        console: The console to print into. Tests pass a `Console`
            with `record=True` and read the captured output.
        quiet: When True, suppresses every event until `on_done`.
    """

    def __init__(self, console: Console, *, quiet: bool = False) -> None:
        self.console = console
        self.quiet = quiet
        self._live: Live | None = None
        self._turn = 0
        self._tool_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0

    # --------------------------------------------------------- lifecycle

    def _ensure_live(self) -> Live | None:
        """Lazy-start the `Live` region. No-op on subsequent calls.

        Returns ``None`` when stdout is not a TTY (pipes, CI logs,
        redirected output) — callers should still print plain per-event
        lines, but the spinner / cursor-control sequences are skipped
        so they don't corrupt log files.
        """
        if not self.console.is_terminal:
            return None
        if self._live is None:
            self._live = Live(
                self._render_status(),
                console=self.console,
                refresh_per_second=8,
                transient=True,
            )
            self._live.start()
        return self._live

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def close(self) -> None:
        """Idempotently stop the `Live` region.

        Safe to call multiple times and from a ``finally`` block — used
        by the CLI to guarantee the cursor / spinner are restored even
        when ``AgentLoop.run`` raises.
        """
        self._stop_live()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render_status())

    def _render_status(self) -> Spinner:
        text = (
            f"Planning · turn {self._turn} · "
            f"{self._tool_calls} tool calls · "
            f"{self._input_tokens + self._output_tokens:,} tokens"
        )
        return Spinner("dots", text=Text(text))

    # ---------------------------------------------------------- events

    def on_turn_start(self, turn: int) -> None:
        self._turn = turn
        if self.quiet:
            return
        self._ensure_live()
        self._refresh()

    def on_tool_call(self, name: str, input: dict[str, Any]) -> None:
        self._tool_calls += 1
        if self.quiet:
            return
        rendered = _format_tool_call(name, input)
        live = self._ensure_live()
        # `Live.console.log` prints above the live region without
        # clobbering the spinner. We use a plain print on the live
        # console instead — log() prefixes a timestamp, which we don't
        # want for the per-call lines.
        target = live.console if live is not None else self.console
        target.print(f"[dim]→[/dim] {rendered}", markup=True)
        self._refresh()

    def on_tool_result(
        self,
        name: str,
        ok: bool,
        summary: str,
        duration_ms: int,
    ) -> None:
        if self.quiet:
            return
        live = self._ensure_live()
        if ok:
            line = f"[green]✓[/green] {escape(summary)} ({duration_ms}ms)"
        else:
            line = f"[red]✗[/red] [red]{escape(summary)}[/red]"
        target = live.console if live is not None else self.console
        target.print(line, markup=True)
        self._refresh()

    def on_turn_complete(
        self,
        turn: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        if self.quiet:
            return
        self._refresh()

    def on_done(
        self,
        total_turns: int,
        total_tool_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        # `on_done` is the moment the spinner becomes misleading — the
        # plan summary printed by the CLI lands right after this. Stop
        # `Live` so the next `console.print` doesn't fight it.
        self._stop_live()


# ---------------------------------------------------------------- helpers


def _format_tool_call(name: str, input_: dict[str, Any]) -> str:
    """Render a single ``→`` line: ``name(arg=val, …)``.

    The tool name and every argument value are passed through
    ``rich.markup.escape`` because the line is later printed with
    ``markup=True`` — model-controlled strings must not be allowed to
    inject Rich markup into the rendered output.
    """
    parts: list[str] = []
    for key, value in input_.items():
        if key == "content":
            # Always omit — usually multi-KB and never useful inline.
            continue
        rendered = _format_arg(key, value)
        parts.append(f"{key}={rendered}")
    return f"{escape(name)}({', '.join(parts)})"


def _format_arg(key: str, value: Any) -> str:
    """Apply per-key truncation rules to a single argument value.

    The returned string is markup-escaped: callers print the assembled
    line with ``markup=True``, so any ``[`` / ``]`` in user-controlled
    values must be neutralised here.
    """
    if key == "path" and isinstance(value, str):
        return escape(repr(os.path.basename(value)))
    if key == "sql" and isinstance(value, str):
        return escape(repr(_truncate(value, _SQL_KEEP)))
    if isinstance(value, str):
        if len(value) > _GENERIC_LIMIT:
            return escape(repr(_truncate(value, _GENERIC_KEEP)))
        return escape(repr(value))
    rendered = repr(value)
    if len(rendered) > _REPR_CAP:
        rendered = rendered[: _REPR_CAP - len(_ELLIPSIS)] + _ELLIPSIS
    return escape(rendered)


def _truncate(text: str, keep: int) -> str:
    if len(text) <= keep:
        return text
    return text[:keep] + _ELLIPSIS


__all__ = ["RichConsoleObserver"]
