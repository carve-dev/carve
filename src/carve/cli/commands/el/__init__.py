"""``carve el`` typer subgroup — operational verbs for EL artifacts.

P1-07 introduced the subgroup with ``run`` and ``list``. P1-08 added
``deploy`` and ``verify``. Keeping every subcommand wired here means
the top-level ``main.py`` only registers ``app`` once.
"""

from __future__ import annotations

import typer

from carve.cli.commands.el import deploy as deploy_cmd
from carve.cli.commands.el import list as list_cmd
from carve.cli.commands.el import run as run_cmd
from carve.cli.commands.el import verify as verify_cmd

app = typer.Typer(
    name="el",
    help="Extract & Load — run, list, deploy, verify EL artifacts.",
    no_args_is_help=True,
)

app.command(name="run")(run_cmd.command)
app.command(name="list")(list_cmd.command)
app.command(name="deploy")(deploy_cmd.command)
app.command(name="verify")(verify_cmd.command)


__all__ = ["app", "deploy_cmd", "list_cmd", "run_cmd", "verify_cmd"]
