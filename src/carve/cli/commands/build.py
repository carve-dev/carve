"""`carve build` — materialise a draft plan into pipeline files."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape as _escape

from carve.cli.orchestrator import build_plan
from carve.cli.orchestrator.builder import BuildError
from carve.cli.orchestrator.observers import RichConsoleObserver
from carve.core.agents.exceptions import AgentError
from carve.core.config import ConfigError, load_config
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

console = Console()


def command(
    plan_id: str = typer.Argument(..., help="ID of the draft plan to build."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Rebuild even if the plan is already in phase=built.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress live progress output; print only the final summary.",
    ),
) -> None:
    """Run the build agent against ``plan_id`` and write pipeline files."""
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

    observer = RichConsoleObserver(console, quiet=quiet)

    try:
        try:
            artifact = build_plan(
                plan_id=plan_id,
                config=config,
                project_dir=project_dir,
                repository=repository,
                observer=observer,
                force=force,
            )
        finally:
            observer.close()
    except BuildError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except AgentError as exc:
        console.print(f"[red]✗ Agent error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    if not artifact.success:
        console.print(
            "[red]✗[/red] Build did not write a `main.py`. Plan stays in "
            "phase=drafted; refine and retry."
        )
        if artifact.summary.strip():
            console.print(artifact.summary.strip())
        raise typer.Exit(code=1)

    console.print(
        f"[green]✓[/green] Built pipeline "
        f"[bold]{_escape(artifact.pipeline_name)}[/bold]"
    )
    console.print(f"  Plan:           {_escape(artifact.plan_id)}")
    console.print(f"  Target:         {_escape(artifact.target)}")
    console.print(
        f"  Build id:       {_escape(artifact.build_id) if artifact.build_id else '(none)'}"
    )
    console.print(f"  Build run id:   {_escape(artifact.run_id)}")
    console.print("  Files written:")
    for path in artifact.files_written:
        console.print(f"    - {path}")
    console.print(
        f"  Tokens:         {artifact.tokens_input:,} in / {artifact.tokens_output:,} out"
    )
    console.print(f"  Cost:           ${artifact.cost_usd:.4f}")
    if artifact.summary.strip():
        console.print()
        console.print(artifact.summary.strip())
    console.print()
    console.print(f"Next:  carve run {artifact.pipeline_name}")
    console.print(
        f"       carve el deploy {artifact.pipeline_name} --to {artifact.target}"
    )
    raise typer.Exit(code=0)
