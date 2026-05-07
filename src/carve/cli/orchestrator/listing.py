"""Renderers for `carve runs`, `carve logs`, and `carve pipelines`.

Each function returns either a `RenderableType` (or `(renderable, exit_code)`)
so the typer command modules stay thin: print the renderable, then exit.
Keeping the rendering logic here makes it trivial to test without spinning
up a typer harness.
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


def render_runs_table(
    repository: Repository,
    *,
    limit: int = 20,
    pipeline_name: str | None = None,
) -> RenderableType:
    """Build a rich `Table` (or empty-state message) for `carve runs`.

    The optional `pipeline_name` filter scopes the listing to a single
    pipeline so `carve runs --pipeline foo` reads as "show foo's runs".
    """
    runs = repository.list_runs(limit=limit, pipeline_name=pipeline_name)
    if not runs:
        if pipeline_name is not None:
            return (
                f"No runs yet for pipeline {pipeline_name!r}. "
                f"Run it with `carve run {pipeline_name}`."
            )
        return "No runs yet. Generate a plan with `carve plan \"<goal>\"` first."

    title = f"Recent runs (last {len(runs)})"
    if pipeline_name is not None:
        title += f" — pipeline {pipeline_name!r}"
    table = Table(title=title)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Kind")
    table.add_column("Pipeline")
    table.add_column("Target")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Duration", justify="right")
    table.add_column("Cost (USD)", justify="right")

    for run in runs:
        table.add_row(
            _short_id(run.id),
            run.kind,
            run.pipeline_name or "-",
            run.target_id,
            _status_cell(run.status),
            _format_started_at(run.started_at, run.created_at),
            _format_duration(run.duration_ms),
            f"${run.cost_usd:.4f}" if run.cost_usd else "-",
        )
    return table


def render_logs(repository: Repository, run_id: str) -> tuple[RenderableType, int]:
    """Render logs for `run_id`. Returns (renderable, exit_code)."""
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


def render_pipelines_table(
    repository: Repository,
    *,
    limit: int = 50,
) -> RenderableType:
    """Build a rich `Table` (or empty-state message) for `carve pipelines`."""
    pipelines = repository.list_pipelines(limit=limit)
    if not pipelines:
        return (
            "No pipelines yet. Generate a plan with `carve plan \"<goal>\"` "
            "and then `carve build <plan_id>`."
        )

    table = Table(title=f"Pipelines (showing {len(pipelines)})")
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Description")
    table.add_column("Current build", style="dim")
    table.add_column("Last run")
    table.add_column("Last run at")
    table.add_column("Updated")

    for pipeline in pipelines:
        # Pipeline name and description originate from the plan agent's
        # `submit_plan` design dict; escape both before they hit Rich's
        # markup parser. Same pattern as M1.1-04.
        table.add_row(
            _escape(pipeline.name),
            _escape(pipeline.description) if pipeline.description else "-",
            _short_id(pipeline.current_build_id) if pipeline.current_build_id else "-",
            _status_cell(pipeline.last_run_status) if pipeline.last_run_status else "-",
            _format_datetime(pipeline.last_run_at),
            _format_datetime(pipeline.updated_at),
        )
    return table


def render_pipeline_detail(
    repository: Repository,
    pipeline_name: str,
) -> tuple[RenderableType, int]:
    """Detailed view: pipeline metadata + plan lineage + recent runs.

    Returns ``(renderable, exit_code)``. Exit code is 1 when the
    pipeline doesn't exist; 0 otherwise.
    """
    lineage = repository.get_pipeline_lineage(pipeline_name)
    if lineage is None:
        return (
            f"[red]✗[/red] Pipeline not found: {_escape(pipeline_name)}",
            1,
        )

    pipeline = lineage.pipeline
    blocks: list[RenderableType] = []

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column()
    # `pipeline.name`, `pipeline.pipeline_dir`, and `pipeline.description`
    # are all derived from the plan agent's design dict; escape before
    # they hit the markup parser.
    summary.add_row("Pipeline", _escape(pipeline.name))
    summary.add_row("Directory", _escape(pipeline.pipeline_dir))
    summary.add_row("Description", _escape(pipeline.description) if pipeline.description else "-")
    summary.add_row(
        "Current build",
        _escape(pipeline.current_build_id) if pipeline.current_build_id else "-",
    )
    if lineage.current_plan is not None:
        summary.add_row("Current plan", _escape(lineage.current_plan.id))
    summary.add_row("Created", _format_datetime(pipeline.created_at))
    summary.add_row("Updated", _format_datetime(pipeline.updated_at))
    summary.add_row(
        "Last run",
        _status_cell(pipeline.last_run_status) if pipeline.last_run_status else "-",
    )
    summary.add_row(
        "Last run at",
        _format_datetime(pipeline.last_run_at),
    )
    blocks.append(summary)

    plans_table = Table(title="Plan lineage")
    plans_table.add_column("Role", no_wrap=True)
    plans_table.add_column("Plan id", style="cyan", no_wrap=True)
    plans_table.add_column("Phase")
    plans_table.add_column("Goal")
    plans_table.add_column("Created")

    for parent in reversed(lineage.parent_chain):
        plans_table.add_row(
            "ancestor",
            parent.id,
            parent.phase,
            _truncate(parent.goal, 60),
            _format_datetime(parent.created_at),
        )
    if lineage.current_plan is not None:
        plans_table.add_row(
            "current",
            f"[bold]{lineage.current_plan.id}[/bold]",
            f"[bold]{lineage.current_plan.phase}[/bold]",
            _truncate(lineage.current_plan.goal, 60),
            _format_datetime(lineage.current_plan.created_at),
        )
    for child in lineage.children:
        plans_table.add_row(
            "refinement",
            child.id,
            child.phase,
            _truncate(child.goal, 60),
            _format_datetime(child.created_at),
        )
    if (
        not lineage.parent_chain
        and not lineage.children
        and lineage.current_plan is None
    ):
        blocks.append("[dim]No plan history recorded.[/dim]")
    else:
        blocks.append(plans_table)

    runs_table = Table(title="Recent runs")
    runs_table.add_column("ID", style="cyan", no_wrap=True)
    runs_table.add_column("Kind")
    runs_table.add_column("Status")
    runs_table.add_column("Started")
    runs_table.add_column("Duration", justify="right")

    if lineage.recent_runs:
        for run in lineage.recent_runs:
            runs_table.add_row(
                _short_id(run.id),
                run.kind,
                _status_cell(run.status),
                _format_started_at(run.started_at, run.created_at),
                _format_duration(run.duration_ms),
            )
        blocks.append(runs_table)
    else:
        blocks.append("[dim]No runs yet.[/dim]")

    return (Group(*blocks), 0)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _short_id(value: str) -> str:
    """Truncate a long id to its first 8 chars for table layout."""
    return value[:8]


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


def _format_datetime(when: datetime | None) -> str:
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
    """Format `[timestamp] [level] [source] message`."""
    if timestamp is None:
        ts = ""
    else:
        ts = timestamp.replace(tzinfo=UTC).strftime("%Y-%m-%d %H:%M:%S")
    return rf"\[{ts}] \[{level}] \[{source}] {message}"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


__all__ = [
    "Run",
    "render_logs",
    "render_pipeline_detail",
    "render_pipelines_table",
    "render_runs_table",
]
