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


def resolve_subcommand_target(subcommand_target: str | None) -> str | None:
    """Combine the subcommand's ``--target`` with the top-level flag.

    The top-level ``carve --target X`` and the subcommand ``carve el run
    <name> --target X`` are both legal. Without this helper typer's
    arg parsing means the subcommand value (often ``None``) silently
    shadows the top-level value — the bug surfaced in dogfooding where
    ``carve --target staging el run iowa`` ran against ``dev``.

    Resolution order: subcommand flag → top-level flag → None (let
    ``resolve_active_target`` fall through to ``CARVE_TARGET`` /
    ``default_target`` / ``"dev"``).
    """
    if subcommand_target is not None and subcommand_target != "":
        return subcommand_target
    # Lazy import to avoid a circular dependency with `cli.main`.
    from carve.cli.main import ACTIVE_TARGET_FLAG

    return ACTIVE_TARGET_FLAG


__all__ = [
    "app",
    "deploy_cmd",
    "list_cmd",
    "resolve_subcommand_target",
    "run_cmd",
    "verify_cmd",
]
