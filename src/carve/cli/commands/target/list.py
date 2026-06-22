"""``carve target list`` — print a table of all defined targets."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from carve.core.config import ConfigError, load_config
from carve.core.targets.registry import (
    list_target_sections,
    section_referenced_env_vars,
)

console = Console()


def command(
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """List all targets defined in carve/connections.toml."""
    root = project_dir.resolve()

    # Try to load config for default_target. If the project isn't
    # initialised yet, fall through with default = "dev" — list still
    # works on bare directories with just a connections.toml.
    default_target = "dev"
    config_dir_name = "carve"
    try:
        config = load_config(root)
        default_target = config.project.default_target
        config_dir_name = config.paths.config_dir
    except ConfigError:
        pass

    conn_path = root / config_dir_name / "connections.toml"
    names = list_target_sections(conn_path)

    if not names:
        console.print(
            "[yellow]No targets yet.[/yellow] Run `carve init` or `carve target create <name>`."
        )
        raise typer.Exit(code=0)

    table = Table(title="Targets", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Default")
    table.add_column("Secrets")

    for name in names:
        is_default = "*" if name == default_target else ""
        secrets_status = _secrets_status(name, conn_path)
        table.add_row(name, is_default, secrets_status)

    console.print(table)
    raise typer.Exit(code=0)


def _secrets_status(name: str, conn_path: Path) -> str:
    """Return a rich-formatted ``✓ all set`` / ``✗ missing`` string."""
    referenced = section_referenced_env_vars(name, conn_path)
    if not referenced:
        # No env vars referenced means the section uses literal values; that
        # counts as "all set" for the purposes of this column.
        return "[green]✓ all set[/green]"
    missing = [var for var in referenced if not os.environ.get(var)]
    if missing:
        return "[red]✗ missing[/red]"
    return "[green]✓ all set[/green]"
