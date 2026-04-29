"""`carve apply` — stub. Real implementation lands in a later milestone."""

import typer
from rich.console import Console

console = Console()


def command(
    plan_id: str = typer.Argument(..., help="ID of the plan to apply."),
) -> None:
    """Apply a previously-generated plan."""
    console.print("[yellow]TODO[/yellow]: apply command not yet implemented")
    console.print(f"Would apply plan: {plan_id}")
    raise typer.Exit(code=0)
