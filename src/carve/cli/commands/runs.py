"""`carve runs` — stub. Real implementation lands in a later milestone."""

import typer
from rich.console import Console

console = Console()


def command() -> None:
    """List recent runs."""
    console.print("[yellow]TODO[/yellow]: runs command not yet implemented")
    raise typer.Exit(code=0)
