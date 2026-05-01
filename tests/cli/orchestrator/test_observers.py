"""Unit tests for `RichConsoleObserver`.

The observer renders into a `rich.console.Console` configured with
``record=True``; tests inspect the captured output via
``console.export_text()``. Live spinner output is filtered out by Rich
when recording, so the per-call ``→``/``✓`` lines are what we assert
on.
"""

from __future__ import annotations

import io

from rich.console import Console

from carve.cli.orchestrator.observers import RichConsoleObserver


def _make_console() -> Console:
    """Build a recording, no-color, fixed-width console for stable output."""
    return Console(
        file=io.StringIO(),
        force_terminal=True,
        width=200,
        record=True,
        no_color=True,
    )


# ---------------------------------------------------------- prints calls


def test_rich_console_observer_prints_tool_calls() -> None:
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=False)

    obs.on_turn_start(1)
    obs.on_tool_call("read_file", {"path": "carve.toml"})
    obs.on_tool_result("read_file", ok=True, summary="42 B read", duration_ms=12)
    obs.on_tool_call(
        "run_snowflake_query",
        {"sql": "SHOW SCHEMAS", "limit": 100},
    )
    obs.on_tool_result(
        "run_snowflake_query", ok=True, summary="14 rows", duration_ms=320
    )
    obs.on_turn_complete(1, input_tokens=100, output_tokens=20)
    obs.on_done(
        total_turns=1,
        total_tool_calls=2,
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.01,
    )

    output = console.export_text()
    assert "read_file(path='carve.toml')" in output
    assert "42 B read" in output
    assert "(12ms)" in output
    assert "run_snowflake_query(sql='SHOW SCHEMAS', limit=100)" in output
    assert "14 rows" in output
    assert "(320ms)" in output


# ---------------------------------------------------------- truncation


def test_rich_console_observer_truncates_long_sql() -> None:
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=False)

    long_sql = "SELECT " + "col, " * 30 + "x FROM long_table_name_here"
    assert len(long_sql) > 60

    obs.on_turn_start(1)
    obs.on_tool_call("run_snowflake_query", {"sql": long_sql})

    output = console.export_text()
    # First 60 chars of the sql must appear, followed by the ellipsis.
    assert long_sql[:60] in output
    assert "…" in output
    # The full sql must NOT appear unwrapped.
    assert long_sql not in output


def test_rich_console_observer_truncates_other_long_strings() -> None:
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=False)

    long_value = "x" * 200
    obs.on_turn_start(1)
    obs.on_tool_call("custom_tool", {"label": long_value})

    output = console.export_text()
    assert long_value not in output
    assert "x" * 60 in output
    assert "…" in output


# ---------------------------------------------------------- omits content


def test_rich_console_observer_omits_content_arg() -> None:
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=False)

    big_content = "y" * 10_000
    obs.on_turn_start(1)
    obs.on_tool_call(
        "write_file",
        {"path": "pipelines/p/main.py", "content": big_content},
    )

    output = console.export_text()
    assert "write_file(path='main.py')" in output
    assert "content" not in output
    assert "y" * 50 not in output


def test_rich_console_observer_basenames_path() -> None:
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=False)

    obs.on_turn_start(1)
    obs.on_tool_call("read_file", {"path": "pipelines/iowa_liquor/main.py"})

    output = console.export_text()
    assert "main.py" in output
    # The directory portion must NOT appear in the rendered call line.
    # (It may appear elsewhere only in headers/summary; we don't print
    # any of those before on_done.)
    assert "pipelines/iowa_liquor/main.py" not in output


# ---------------------------------------------------------- failure path


def test_rich_console_observer_renders_failure() -> None:
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=False)

    obs.on_turn_start(1)
    obs.on_tool_call("read_file", {"path": "missing.toml"})
    obs.on_tool_result(
        "read_file", ok=False, summary="File not found: missing.toml", duration_ms=3
    )

    output = console.export_text()
    assert "✗" in output
    assert "File not found: missing.toml" in output


# ---------------------------------------------------------- quiet mode


def test_quiet_mode_only_prints_summary() -> None:
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=True)

    obs.on_turn_start(1)
    obs.on_tool_call("read_file", {"path": "carve.toml"})
    obs.on_tool_result("read_file", ok=True, summary="ok", duration_ms=5)
    obs.on_turn_complete(1, input_tokens=10, output_tokens=5)

    # Nothing should have been printed yet — the live region is off and
    # the per-call lines are suppressed.
    pre_done = console.export_text()
    assert pre_done.strip() == ""

    obs.on_done(
        total_turns=1,
        total_tool_calls=1,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.0001,
    )
    # `on_done` itself is silent in this observer — the CLI prints the
    # plan summary right after. So the output remains empty.
    assert console.export_text().strip() == ""


# ---------------------------------------------------------- markup safety


def test_rich_console_observer_escapes_markup_in_tool_call() -> None:
    """Model-controlled tool name and arg values must not inject markup.

    The per-call line is printed with ``markup=True``; without escaping,
    a tool name like ``read_file[red]evil[/red]`` or an arg value like
    ``/tmp/[bold]x[/bold]`` would be interpreted as Rich markup. The
    bracketed substrings must survive literally in the captured output.
    """
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=False)

    obs.on_turn_start(1)
    # NB: ``path`` is a special-cased argument key that runs through
    # ``os.path.basename`` and would consume the closing ``[/bold]``
    # tag. Pass the markup-bearing value under a non-special key so the
    # full bracketed payload reaches the renderer intact.
    obs.on_tool_call(
        "read_file[red]evil[/red]",
        {"label": "/tmp/[bold]x[/bold]"},
    )

    output = console.export_text()
    assert "[red]evil[/red]" in output
    assert "[bold]x[/bold]" in output


# ---------------------------------------------------------- live cleanup


def test_close_stops_live_region() -> None:
    """`close()` must tear down an active `Live` region."""
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=False)

    # Trigger live-region start via a turn-start event.
    obs.on_turn_start(1)
    assert obs._live is not None

    obs.close()
    assert obs._live is None


def test_close_is_idempotent() -> None:
    """`close()` must be safe to call multiple times, including with no live."""
    console = _make_console()
    obs = RichConsoleObserver(console, quiet=False)

    # No live region started yet — close should still be a no-op.
    obs.close()
    assert obs._live is None

    obs.on_turn_start(1)
    assert obs._live is not None

    obs.close()
    obs.close()  # second call must not raise
    assert obs._live is None


# ---------------------------------------------------------- non-tty fallback


def test_non_tty_console_skips_live_region_but_still_prints() -> None:
    """When stdout is not a TTY, no `Live` region is started — but the
    per-call lines still appear as plain output so log files / piped
    output remain readable.
    """
    console = Console(
        file=io.StringIO(),
        force_terminal=False,
        width=200,
        record=True,
        no_color=True,
    )
    obs = RichConsoleObserver(console, quiet=False)

    obs.on_turn_start(1)
    # No live region should have been started.
    assert obs._live is None

    obs.on_tool_call("read_file", {"path": "carve.toml"})
    obs.on_tool_result("read_file", ok=True, summary="42 B read", duration_ms=12)

    # Still no live region — the spinner is suppressed in non-tty mode.
    assert obs._live is None

    output = console.export_text()
    assert "read_file(path='carve.toml')" in output
    assert "42 B read" in output
    assert "(12ms)" in output
