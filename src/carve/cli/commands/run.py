"""`carve run` — stub. Real implementation lands in a later milestone."""

import typer
from rich.console import Console

console = Console()


def command(
    pipeline: str = typer.Argument(..., help="Pipeline to run."),
) -> None:
    """Run a pipeline."""
    console.print("[yellow]TODO[/yellow]: run command not yet implemented")
    console.print(f"Would run pipeline: {pipeline}")
    raise typer.Exit(code=0)
