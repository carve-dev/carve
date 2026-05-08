"""`carve runs` — list recent runs from the state store.

P1-09 added ``--recovery <run_id>`` for rendering the chain of
recovery attempts attached to a parent run as a tree. The tree shows
the parent failure on top, each child attempt's diagnosis and outcome
underneath.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from carve.cli.orchestrator import render_recovery_tree, render_runs_table
from carve.core.config import ConfigError, load_config
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

console = Console()


def command(
    limit: int = typer.Option(20, help="Maximum number of runs to show."),
    pipeline: str | None = typer.Option(
        None,
        "--pipeline",
        help="Filter to runs of this pipeline.",
    ),
    recovery: str | None = typer.Option(
        None,
        "--recovery",
        help=(
            "Render the recovery-attempt chain attached to this run id "
            "as a tree (parent + children + diagnoses)."
        ),
    ),
) -> None:
    """List recent runs (or render a recovery chain with --recovery)."""
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

    exit_code = 0
    try:
        if recovery is not None:
            renderable, exit_code = render_recovery_tree(repository, recovery)
            console.print(renderable)
        else:
            renderable = render_runs_table(
                repository, limit=limit, pipeline_name=pipeline
            )
            console.print(renderable)
    finally:
        engine.dispose()

    raise typer.Exit(code=exit_code)
