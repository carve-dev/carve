"""``carve target show`` — print details of a single target.

P1.1-01 removed the per-target filesystem tree; this command now prints
connection config only. EL artifacts under ``el/`` are shared across
targets, so a per-artifact-listing here would duplicate ``carve el list``
without adding signal.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console

from carve.core.config import ConfigError, load_config
from carve.core.targets.registry import (
    InvalidTargetNameError,
    TargetNotFoundError,
    list_target_sections,
    section_referenced_env_vars,
    show_section_values,
    validate_target_name,
)

console = Console()


def command(
    name: str = typer.Argument(..., help="Target name to show."),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Print connection summary for ``<name>``."""
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

    if name not in list_target_sections(conn_path):
        console.print(f'[red]Error:[/red] target "{name}" not defined in {conn_path}.')
        raise typer.Exit(code=2)

    try:
        values = show_section_values(name, conn_path)
    except TargetNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    referenced = section_referenced_env_vars(name, conn_path)
    set_vars = [v for v in referenced if os.environ.get(v)]
    missing_vars = [v for v in referenced if not os.environ.get(v)]

    console.print(f"[bold]Target:[/bold] {name}")
    console.print(f"  Default:        {'yes' if name == default_target else 'no'}")
    console.print(f"  Defined in:     {conn_path.relative_to(root)} [snowflake.{name}]")
    if not referenced:
        console.print("  Secrets:        (literal values; no env vars referenced)")
    elif missing_vars:
        console.print(
            f"  Secrets:        [red]✗ missing[/red] "
            f"({', '.join(sorted(missing_vars))})"
        )
    else:
        console.print(
            f"  Secrets:        [green]✓ all set[/green] "
            f"({', '.join(sorted(set_vars))})"
        )
    console.print()
    console.print("[bold]Connection (resolved)[/bold]")
    console.print(f"  snowflake.{name}:")
    for value in values:
        if value.env_var is not None:
            display = f"<from {value.env_var}>"
        else:
            display = value.raw
        console.print(f"    {value.key}: {display}")
    console.print()
    console.print(
        "[dim]EL artifacts are shared across targets — run "
        "`carve el list` to see them.[/dim]"
    )
    raise typer.Exit(code=0)
