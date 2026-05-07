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
    targets_dir_name = "targets"
    try:
        config = load_config(root)
        default_target = config.project.default_target
        config_dir_name = config.paths.config_dir
        targets_dir_name = config.paths.targets_dir
    except ConfigError:
        pass

    conn_path = root / config_dir_name / "connections.toml"
    targets_root = root / targets_dir_name
    names = list_target_sections(conn_path)

    if not names:
        console.print(
            "[yellow]No targets yet.[/yellow] "
            "Run `carve init` or `carve target create <name>`."
        )
        raise typer.Exit(code=0)

    table = Table(title="Targets", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Default")
    table.add_column("Secrets")
    table.add_column("Artifacts dir")
    table.add_column("EL artifacts", justify="right")

    for name in names:
        is_default = "*" if name == default_target else ""
        secrets_status = _secrets_status(name, conn_path)
        artifacts_dir = targets_root / name
        if artifacts_dir.is_dir():
            artifacts_status = "[green]✓ exists[/green]"
            el_count = _count_el_artifacts(artifacts_dir)
            el_display = str(el_count)
        else:
            artifacts_status = "[red]✗ missing[/red]"
            el_display = "—"
        table.add_row(name, is_default, secrets_status, artifacts_status, el_display)

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


def _count_el_artifacts(artifacts_dir: Path) -> int:
    """Count subdirectories under ``targets/<name>/el/``."""
    el_dir = artifacts_dir / "el"
    if not el_dir.is_dir():
        return 0
    return sum(1 for child in el_dir.iterdir() if child.is_dir())
