"""``carve target`` typer subgroup."""

from __future__ import annotations

import typer

from carve.cli.commands.target import create, delete, rename, show
from carve.cli.commands.target import list as list_

app = typer.Typer(
    name="target",
    help="Manage Carve targets (dev, staging, prod, …).",
    no_args_is_help=True,
)

app.command(name="create")(create.command)
app.command(name="list")(list_.command)
app.command(name="show")(show.command)
app.command(name="rename")(rename.command)
app.command(name="delete")(delete.command)


__all__ = ["app"]
