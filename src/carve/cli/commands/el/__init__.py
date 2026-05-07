"""``carve el`` typer subgroup — operational verbs for EL artifacts.

P1-07 introduces the subgroup with ``run`` and ``list`` subcommands.
P1-08 will add ``deploy`` and ``verify``. Keeping the subgroup wired
here means the top-level ``main.py`` only registers ``app`` once.
"""

from __future__ import annotations

import typer

from carve.cli.commands.el import list as list_cmd
from carve.cli.commands.el import run as run_cmd

app = typer.Typer(
    name="el",
    help="Extract & Load — run, list, (later) deploy, verify EL artifacts.",
    no_args_is_help=True,
)

app.command(name="run")(run_cmd.command)
app.command(name="list")(list_cmd.command)


__all__ = ["app", "list_cmd", "run_cmd"]
