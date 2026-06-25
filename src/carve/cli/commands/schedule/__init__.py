"""``carve schedule`` — the narrow code→data re-seed bridge.

This unit ships only ``carve schedule reseed <pipeline>`` as a **deferred
stub**. ``reseed`` re-applies a pipeline's ``[seed_schedule]`` block to the
live ``schedules`` row — but that table does not exist yet (it is the
Increment-4 runtime's), so there is nothing to re-apply onto. The command
exits non-zero with a clear "not available yet" message rather than silently
no-opping.

The everyday schedule controls (``carve schedule list/show/pause/resume``) are
the runtime's (they operate on the ``schedules`` table as data); they are out
of scope here and deliberately not registered.
"""

from __future__ import annotations

import typer
from rich.console import Console

console = Console()

app = typer.Typer(
    name="schedule",
    help="Re-seed a pipeline's schedule from its [seed_schedule] block.",
    no_args_is_help=True,
)


@app.command(name="reseed")
def reseed(
    pipeline: str = typer.Argument(..., help="Pipeline whose [seed_schedule] to re-apply."),
) -> None:
    """Re-apply a pipeline's [seed_schedule] to the schedules table. DEFERRED."""
    console.print(
        f"[yellow]carve schedule reseed {pipeline} is not available yet — the live "
        "schedules table is owned by the runtime (Increment 4). "
        r"\[seed_schedule] is seeded at first registration once the runtime "
        "ships.[/yellow]"
    )
    raise typer.Exit(code=1)


__all__ = ["app"]
