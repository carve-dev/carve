"""``carve schedule`` — the live schedule surface (data) + the deferred reseed bridge.

The everyday controls operate on the ``schedules`` table as **data** (the
scheduler's source of truth):

* ``carve schedule list`` / ``show`` — read the live schedule + its audit trail.
* ``carve schedule pause`` / ``resume`` — gate firing instantly (audited).
* ``carve schedule set-cron`` — change (or stand up) a schedule's cron instantly
  (audited); UPSERTs so a schedule can be created without the reconciler-seed.

``carve schedule reseed <pipeline>`` stays a **deferred stub**: it re-applies a
pipeline's ``[seed_schedule]`` block onto the live row, which is the PIPELINES
reconciler's job (spec 08) — it exits non-zero with a clear "not available yet"
message rather than silently no-opping.
"""

from __future__ import annotations

import typer
from rich.console import Console

from carve.cli.commands.schedule.commands import (
    list_command,
    pause_command,
    resume_command,
    set_cron_command,
    show_command,
)

console = Console()

app = typer.Typer(
    name="schedule",
    help="List, show, pause, resume, and set the cron of live schedules.",
    no_args_is_help=True,
)

app.command(name="list")(list_command)
app.command(name="show")(show_command)
app.command(name="pause")(pause_command)
app.command(name="resume")(resume_command)
app.command(name="set-cron")(set_cron_command)


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
