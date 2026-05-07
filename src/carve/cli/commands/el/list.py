"""``carve el list`` — table view of EL artifacts in the active target.

One row per directory under ``targets/<active>/el/``. Pulls last-build
and last-run state from the state store; relative-time columns make
the output skim-friendly for daily-driver use.
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


_STATUS_GLYPHS: dict[str, str] = {
    "success": "[green]✓ success[/green]",
    "failed": "[red]✗ failed[/red]",
    "cancelled": "[magenta]⊘ cancelled[/magenta]",
    "crashed": "[red]✗ crashed[/red]",
    "running": "[cyan]⟳ running[/cyan]",
    "queued": "[yellow]queued[/yellow]",
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
    harness or a real engine.
    """
    el_dir = project_dir / "targets" / active_target / "el"
    artifact_names: list[str] = []
    if el_dir.is_dir():
        for entry in sorted(el_dir.iterdir()):
            if entry.is_dir() and (entry / "main.py").is_file():
                artifact_names.append(entry.name)

    if not artifact_names:
        return (
            f"No EL artifacts in target '{active_target}'. "
            f"Run carve plan ... to create one."
        )

    table = Table(title=f'EL artifacts in target "{active_target}"')
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Built")
    table.add_column("Last run")
    table.add_column("Status")

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
        last_run_status: str | None = None
        if pipeline is not None:
            last_run_at = pipeline.last_run_at
            last_run_status = pipeline.last_run_status
        last_run_str = (
            _format_relative(last_run_at, now) if last_run_at is not None else "never"
        )
        status_str = _STATUS_GLYPHS.get(last_run_status or "", "-")

        table.add_row(
            _escape(name),
            built_str,
            last_run_str,
            status_str,
        )

    return table


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
