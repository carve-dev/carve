"""`carve logs` — stub. Real implementation lands in a later milestone."""

import typer
from rich.console import Console

console = Console()


def command(
    run_id: str = typer.Argument(..., help="ID of the run to fetch logs for."),
) -> None:
    """Show logs for a run."""
    console.print("[yellow]TODO[/yellow]: logs command not yet implemented")
    console.print(f"Would show logs for run: {run_id}")
    raise typer.Exit(code=0)
