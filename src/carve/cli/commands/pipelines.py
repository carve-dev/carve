"""`carve pipelines` — list pipelines, or show a single pipeline's lineage."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from carve.cli.orchestrator import (
    render_pipeline_detail,
    render_pipelines_table,
)
from carve.core.config import ConfigError, load_config
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

console = Console()


def command(
    name: str | None = typer.Argument(
        None,
        help="Pipeline name to inspect. Omit to list all pipelines.",
    ),
) -> None:
    """List pipelines or show a single pipeline's lineage."""
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
        if name is None:
            renderable = render_pipelines_table(repository)
            console.print(renderable)
            exit_code = 0
        else:
            renderable, exit_code = render_pipeline_detail(repository, name)
            console.print(renderable)
    finally:
        engine.dispose()

    raise typer.Exit(code=exit_code)
