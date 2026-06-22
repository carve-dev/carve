"""``carve target delete`` — remove a target."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from carve.core.config import ConfigError, load_config
from carve.core.targets.registry import (
    InvalidTargetNameError,
    TargetNotFoundError,
    list_target_sections,
    remove_env_example_block,
    remove_target_section,
    validate_target_name,
)

console = Console()


def command(
    name: str = typer.Argument(..., help="Target name to delete."),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow deleting the default target (with --no-default-warning).",
    ),
    no_default_warning: bool = typer.Option(
        False,
        "--no-default-warning",
        help="Together with --force, allows deleting the default target.",
    ),
) -> None:
    """Remove a target's connections.toml section and .env.example block.

    P1.1-01 removed the per-target filesystem tree, so this command no
    longer touches ``targets/<name>/``. If a legacy ``targets/<name>/``
    directory exists from a pre-P1.1 project, it's left in place; users
    can ``rm -rf`` it themselves.
    """
    try:
        validate_target_name(name)
    except InvalidTargetNameError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    root = project_dir.resolve()

    default_target = "dev"
    config_dir_name = "carve"
    try:
        config = load_config(root)
        default_target = config.project.default_target
        config_dir_name = config.paths.config_dir
    except ConfigError:
        pass

    conn_path = root / config_dir_name / "connections.toml"
    env_example_path = root / ".env.example"

    if name not in list_target_sections(conn_path):
        console.print(f'[red]Error:[/red] target "{name}" not defined in {conn_path}.')
        raise typer.Exit(code=2)

    # Safety rail: refuse to delete default_target without --force AND
    # --no-default-warning.
    if name == default_target and not (force and no_default_warning):
        console.print(
            f'[red]Error:[/red] "{name}" is the default target. '
            f"Pass [bold]--force --no-default-warning[/bold] to delete anyway."
        )
        raise typer.Exit(code=2)

    if not yes:
        message = f'Delete target "{name}" — section in connections.toml and lines in .env.example?'
        if not typer.confirm(message, default=False):
            console.print("Aborted.")
            raise typer.Exit(code=1)

    # 1) Remove the section.
    try:
        remove_target_section(name, conn_path)
    except TargetNotFoundError as exc:
        # Race condition (someone else removed it): treat as success.
        console.print(f"[yellow]Warning:[/yellow] {exc}")

    # 2) Remove the .env.example block.
    remove_env_example_block(name, env_example_path)

    console.print(f'[green]Deleted target "{name}".[/green]')
    console.print(
        f"\n[yellow]Reminder:[/yellow] remove {name.upper()}_* lines from your "
        f"local .env (Carve does not edit .env)."
    )
    raise typer.Exit(code=0)
