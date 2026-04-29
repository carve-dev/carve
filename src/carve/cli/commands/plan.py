"""`carve plan` — stub. Real implementation lands in a later milestone."""

import typer
from rich.console import Console

console = Console()


def command(
    goal: str = typer.Argument(..., help="The goal for the agent."),
) -> None:
    """Generate a plan for the given goal."""
    console.print("[yellow]TODO[/yellow]: plan command not yet implemented")
    console.print(f"Would generate plan for goal: {goal}")
    raise typer.Exit(code=0)
