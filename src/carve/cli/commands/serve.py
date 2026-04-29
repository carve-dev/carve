"""`carve serve` — stub. Real implementation lands in a later milestone."""

import typer
from rich.console import Console

console = Console()


def command(
    host: str = typer.Option("127.0.0.1", help="Host to bind to."),
    port: int = typer.Option(8080, help="Port to bind to."),
) -> None:
    """Run the Carve API server and web UI."""
    console.print("[yellow]TODO[/yellow]: serve command not yet implemented")
    console.print(f"Would serve on {host}:{port}")
    raise typer.Exit(code=0)
