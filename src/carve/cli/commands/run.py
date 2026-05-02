"""`carve run` — execute a pipeline by name (or by plan id for replay)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from carve.cli.orchestrator import (
    run_pipeline_by_name,
    run_pipeline_by_plan,
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
    pipeline: str | None = typer.Argument(
        None,
        help="Pipeline name to run.",
    ),
    plan: str | None = typer.Option(
        None,
        "--plan",
        help="Debug-replay: run the pipeline that this plan id built.",
    ),
) -> None:
    """Run a pipeline."""
    if (pipeline is None) == (plan is None):
        console.print(
            "[red]✗[/red] Provide exactly one of <pipeline_name> or `--plan <plan_id>`."
        )
        raise typer.Exit(code=2)

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
        if plan is not None:
            exit_code = run_pipeline_by_plan(
                plan_id=plan,
                config=config,
                project_dir=project_dir,
                repository=repository,
                console=console,
            )
        else:
            assert pipeline is not None  # narrowed by the XOR check above
            exit_code = run_pipeline_by_name(
                pipeline_name=pipeline,
                config=config,
                project_dir=project_dir,
                repository=repository,
                console=console,
            )
    finally:
        engine.dispose()

    raise typer.Exit(code=exit_code)
