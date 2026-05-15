"""``carve el list`` — table view of EL artifacts.

One row per directory under ``el/``. Pulls last-build and last-run
state from the state store; the "Last run" column rolls up the per-
target results so a single artifact can show
``dev=success prod=failed`` at a glance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape as _escape
from rich.table import Table

from carve.core.config import ConfigError, load_config
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

console = Console()


_PLAIN_STATUS_LABELS: dict[str, str] = {
    "success": "success",
    "failed": "failed",
    "cancelled": "cancelled",
    "crashed": "crashed",
    "running": "running",
    "queued": "queued",
}


def command(
    target: str | None = typer.Option(
        None,
        "--target",
        help="Override the active target (defaults to carve.toml's default_target).",
    ),
) -> None:
    """List EL artifacts in the active target."""
    project_dir = Path.cwd()

    from carve.cli.commands.el import resolve_subcommand_target

    target = resolve_subcommand_target(target)

    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    from carve.core.targets.resolution import (
        TargetResolutionError,
        resolve_active_target,
    )

    try:
        active_target = resolve_active_target(target, config)
    except TargetResolutionError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    repository = Repository(session_factory)

    try:
        renderable = render_el_list(
            repository=repository,
            project_dir=project_dir,
            active_target=active_target,
        )
        console.print(renderable)
    finally:
        engine.dispose()


def render_el_list(
    *,
    repository: Repository,
    project_dir: Path,
    active_target: str,
) -> object:
    """Build the table (or empty-state string) for the listing.

    Pulled out of `command` so tests can drive it without the typer
    harness or a real engine. ``active_target`` is retained for build-
    age relative-time display (the "Built" column shows the most
    recent build for the active target); the rollup column iterates
    every distinct target observed in the `runs` table for each
    artifact.
    """
    el_dir = project_dir / "el"
    artifact_names: list[str] = []
    if el_dir.is_dir():
        for entry in sorted(el_dir.iterdir()):
            if entry.is_dir() and (entry / "main.py").is_file():
                artifact_names.append(entry.name)

    if not artifact_names:
        return (
            "No EL artifacts under el/. "
            "Run carve plan ... to create one."
        )

    table = Table(title="EL artifacts")
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Built")
    table.add_column("Last run")
    table.add_column("Per-target status")

    now = datetime.now(UTC).replace(tzinfo=None)
    for name in artifact_names:
        latest_build = repository.latest_build_for(name, active_target)
        built_str = (
            _format_relative(latest_build.created_at, now)
            if latest_build is not None
            else "-"
        )

        pipeline = repository.get_pipeline(name)
        last_run_at: datetime | None = None
        if pipeline is not None:
            last_run_at = pipeline.last_run_at
        last_run_str = (
            _format_relative(last_run_at, now) if last_run_at is not None else "never"
        )

        rollup = _per_target_rollup(repository, name)

        table.add_row(
            _escape(name),
            built_str,
            last_run_str,
            rollup,
        )

    return table


def _per_target_rollup(repository: Repository, name: str) -> str:
    """Return a ``dev=success prod=failed`` per-target last-run string.

    Walks the artifact's run history (kind=``run``) and picks the
    latest status per ``runs.target``. Targets are listed alphabetically
    for a stable display order. Returns ``"-"`` when no runs exist.
    """
    # Pull a generous slice; the per-target rollup uses each target's
    # latest entry, so 200 covers normal usage even for hot artifacts.
    runs = repository.list_runs(limit=200, pipeline_name=name)
    latest_per_target: dict[str, str] = {}
    # list_runs returns newest first; the first hit for each target is
    # the latest run for that target.
    for run in runs:
        if run.kind != "run":
            continue
        target = run.target
        if not target:
            continue
        if target in latest_per_target:
            continue
        latest_per_target[target] = run.status

    if not latest_per_target:
        return "-"

    parts: list[str] = []
    for target in sorted(latest_per_target):
        status = latest_per_target[target]
        label = _PLAIN_STATUS_LABELS.get(status, status)
        if status == "success":
            parts.append(f"[green]{target}={label}[/green]")
        elif status in ("failed", "crashed"):
            parts.append(f"[red]{target}={label}[/red]")
        elif status == "cancelled":
            parts.append(f"[magenta]{target}={label}[/magenta]")
        elif status == "running":
            parts.append(f"[cyan]{target}={label}[/cyan]")
        else:
            parts.append(f"{target}={label}")
    return " ".join(parts)


def _format_relative(when: datetime, now: datetime) -> str:
    """Format a wall-clock datetime as a coarse relative string.

    The granularity intentionally tops out at "days ago" — for an EL
    listing the user wants "is this fresh or stale", not millisecond
    precision.
    """
    if when.tzinfo is not None:
        when = when.astimezone(UTC).replace(tzinfo=None)
    delta = now - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        # Clock skew on the test fixture; treat as "just now".
        return "just now"
    if seconds < 60:
        return "just now" if seconds < 30 else "less than a minute ago"
    minutes = seconds // 60
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} ago"
    hours = minutes // 60
    if hours < 24:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    days = hours // 24
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} ago"


__all__ = ["command", "render_el_list"]
