"""`carve deploy` — deprecated alias for `carve el deploy`.

P1-08 introduced ``carve el deploy``; the root-level ``carve deploy``
verb is retained as a hidden alias for one minor version with a
yellow deprecation banner. Removed in v0.2.

The forwarding signature mirrors `carve el deploy`'s public surface
so existing scripts that pass ``--from`` / ``--to`` keep working
without translation.
"""

from __future__ import annotations

import typer
from rich.console import Console

from carve.cli.commands.el import deploy as el_deploy

console = Console()


def command(
    name: str = typer.Argument(..., help="EL artifact name to deploy."),
    from_target: str = typer.Option(
        ...,
        "--from",
        help="Source target (forwarded to `carve el deploy`).",
    ),
    to_target: str = typer.Option(
        ...,
        "--to",
        help="Destination target (forwarded to `carve el deploy`).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the confirmation prompt before any writes.",
    ),
    no_smoke_test: bool = typer.Option(
        False,
        "--no-smoke-test",
        help="Skip the post-DDL `SELECT 1` queryability check.",
    ),
    no_auto_fix: bool = typer.Option(
        False,
        "--no-auto-fix",
        help="Disable the recovery agent.",
    ),
) -> None:
    """Deprecated: forwards to ``carve el deploy``.

    Removed in v0.2. The first thing this command does is print a
    yellow deprecation banner so scripted callers see the migration
    notice in their CI logs.
    """
    console.print(
        "[yellow]`carve deploy` is deprecated; use `carve el deploy` instead.[/yellow]\n"
        "[yellow]This alias will be removed in v0.2.[/yellow]"
    )
    el_deploy.command(
        name=name,
        from_target=from_target,
        to_target=to_target,
        yes=yes,
        no_smoke_test=no_smoke_test,
        no_auto_fix=no_auto_fix,
    )


__all__ = ["command"]
