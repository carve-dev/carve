"""`carve apply` — execute a previously-generated plan."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from carve.cli.orchestrator import apply_plan
from carve.core.config import ConfigError, load_config
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

console = Console()


def command(
    plan_id: str = typer.Argument(..., help="ID of the plan to apply."),
) -> None:
    """Apply a previously-generated plan."""
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
        exit_code = apply_plan(
            plan_id=plan_id,
            config=config,
            project_dir=project_dir,
            repository=repository,
            console=console,
        )
    finally:
        engine.dispose()

    raise typer.Exit(code=exit_code)
