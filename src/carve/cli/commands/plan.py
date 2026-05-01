"""`carve plan` — generate a pipeline plan from a natural-language goal."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape as _escape

from carve.cli.orchestrator import generate_plan
from carve.cli.orchestrator.observers import RichConsoleObserver
from carve.cli.orchestrator.planner import PlanGenerationError
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
    goal: str = typer.Argument(..., help="The goal for the agent."),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress live progress output; print only the final summary.",
    ),
) -> None:
    """Generate a plan for the given goal."""
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
            artifact = generate_plan(
                goal=goal,
                config=config,
                project_dir=project_dir,
                repository=repository,
                observer=observer,
            )
        finally:
            # Always tear down the live spinner, even if `generate_plan`
            # raised — otherwise the cursor stays hidden and subsequent
            # error output collides with the half-drawn live region.
            observer.close()
    except PlanGenerationError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except AgentError as exc:
        console.print(f"[red]✗ Agent error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    _render_plan_summary(artifact)
    raise typer.Exit(code=0)


def _render_plan_summary(artifact: object) -> None:
    """Print the human-friendly plan summary.

    Mirrors the layout described in the milestone-1 acceptance flow:
    plan id, pipeline path, requirements, token cost, and a wrapped
    body of the agent's final assistant text.
    """
    # Late import keeps the typer entry-point lean for `--help` invocations.
    from carve.cli.orchestrator.planner import PlanArtifact

    assert isinstance(artifact, PlanArtifact)
    plan_id = _escape(artifact.id)
    console.print(f"[green]✓[/green] Plan generated: [bold]{plan_id}[/bold]")
    console.print(f"  Pipeline:     {_escape(artifact.script_path)}")
    if artifact.requirements:
        console.print(f"  Requirements: {', '.join(artifact.requirements)}")
    else:
        console.print("  Requirements: (none)")
    console.print(
        f"  Tokens:       {artifact.tokens_input:,} in / {artifact.tokens_output:,} out"
    )
    console.print(f"  Cost:         ${artifact.cost_usd:.4f}")
    if artifact.summary.strip():
        console.print("Summary:")
        for line in artifact.summary.strip().splitlines():
            console.print(f"  {line}")
    console.print(f"Run with:     carve apply {plan_id}")
