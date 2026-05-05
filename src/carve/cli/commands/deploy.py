"""`carve deploy` — M2 placeholder.

The verb is reserved for "promote this pipeline to prod via PR" (M2).
For dev execution use `carve run`. The placeholder exits 0 so scripts
that pre-emptively wire the call don't break.
"""

from __future__ import annotations

import typer
from rich.console import Console

console = Console()


def command(
    pipeline: str = typer.Argument(..., help="Pipeline name to deploy."),
) -> None:
    """Deploy a pipeline to prod (M2)."""
    console.print(
        f"carve deploy will create a prod-deploy PR for pipeline "
        f"{pipeline!r} (arrives in M2)."
    )
    console.print(f"For dev execution, use:  carve run {pipeline}")
    raise typer.Exit(code=0)
