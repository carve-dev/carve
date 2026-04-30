"""`carve logs` — print logs for a single run."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from carve.cli.orchestrator import render_logs
from carve.core.config import ConfigError, load_config
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

console = Console()


def command(
    run_id: str = typer.Argument(..., help="ID of the run to fetch logs for."),
) -> None:
    """Show logs for a run."""
    project_dir = Path.cwd()

    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    repository = Repository(session_factory)

    try:
        renderable, exit_code = render_logs(repository, run_id)
        console.print(renderable)
    finally:
        engine.dispose()

    raise typer.Exit(code=exit_code)
