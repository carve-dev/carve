"""`carve version` — print the package version."""

import typer
from rich.console import Console

from carve.version import __version__

console = Console()


def command() -> None:
    """Print the carve version."""
    console.print(__version__)
    raise typer.Exit(code=0)
