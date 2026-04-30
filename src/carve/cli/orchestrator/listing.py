"""Renderers for `carve runs` and `carve logs`.

Both functions return a tuple of (renderable, exit_code) so the typer
command modules can stay thin: print the renderable, then exit. Keeping
the rendering logic here makes it trivial to test without spinning up a
typer harness.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Group, RenderableType
from rich.markup import escape as _escape
from rich.table import Table

from carve.core.state import Repository, Run

# Status -> rich colour. Unknown statuses fall through to plain text.
_STATUS_COLORS: dict[str, str] = {
    "queued": "yellow",
    "running": "cyan",
    "success": "green",
    "failed": "red",
    "cancelled": "magenta",
    "crashed": "red",
}


def render_runs_table(repository: Repository, *, limit: int = 20) -> RenderableType:
    """Build a rich `Table` (or empty-state message) for `carve runs`."""
    runs = repository.list_runs(limit=limit)
    if not runs:
        return "No runs yet. Generate a plan with `carve plan \"<goal>\"` first."

    table = Table(title=f"Recent runs (last {len(runs)})")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Kind")
    table.add_column("Target")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Duration", justify="right")
    table.add_column("Cost (USD)", justify="right")

    for run in runs:
        table.add_row(
            _short_id(run.id),
            run.kind,
            run.target_id,
            _status_cell(run.status),
            _format_started_at(run.started_at, run.created_at),
            _format_duration(run.duration_ms),
            f"${run.cost_usd:.4f}" if run.cost_usd else "-",
        )
    return table


def render_logs(repository: Repository, run_id: str) -> tuple[RenderableType, int]:
    """Render logs for `run_id`. Returns (renderable, exit_code).

    Exit code is 1 when the run doesn't exist, 0 otherwise — including
    the empty-logs case, since "the run exists but produced no output"
    is a valid terminal state.
    """
    run = repository.get_run(run_id)
    if run is None:
        return (f"[red]✗[/red] Run not found: {_escape(run_id)}", 1)

    logs = repository.get_logs(run_id)
    if not logs:
        return (
            f"[dim]No logs recorded for run {_escape(run_id)} "
            f"(status: {run.status}).[/dim]",
            0,
        )
    lines = [_format_log_line(log.timestamp, log.level, log.source, log.message) for log in logs]
    return (Group(*lines), 0)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _short_id(run_id: str) -> str:
    """Truncate a run id to its first 8 hex chars for table layout."""
    return run_id[:8]


def _status_cell(status: str) -> str:
    color = _STATUS_COLORS.get(status)
    if color is None:
        return status
    return f"[{color}]{status}[/{color}]"


def _format_started_at(started_at: datetime | None, created_at: datetime) -> str:
    """Use started_at when available; fall back to created_at."""
    when = started_at if started_at is not None else created_at
    if when is None:
        return "-"
    return when.replace(tzinfo=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(duration_ms: int | None) -> str:
    """Render a duration as a short human string."""
    if duration_ms is None:
        return "-"
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    return f"{duration_ms / 1000:.1f}s"


def _format_log_line(timestamp: datetime, level: str, source: str, message: str) -> str:
    """Format `[timestamp] [level] [source] message`.

    Rich treats `[level]` as markup, so each bracket pair is escaped via
    a leading backslash so it renders literally and tests can match on
    the visible string.
    """
    if timestamp is None:
        ts = ""
    else:
        ts = timestamp.replace(tzinfo=UTC).strftime("%Y-%m-%d %H:%M:%S")
    return rf"\[{ts}] \[{level}] \[{source}] {message}"


# Avoid unused-import warning when only certain helpers are exercised by
# tests; `Run` is part of the typed surface for callers building their
# own table rows.
__all__ = ["Run", "render_logs", "render_runs_table"]
