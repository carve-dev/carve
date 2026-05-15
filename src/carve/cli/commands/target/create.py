"""``carve target create`` — add a new target.

Adds a ``[snowflake.<name>]`` section to ``carve/connections.toml`` and
appends a ``# === <name> target ===`` block to ``.env.example``. Both are
produced by the single ``add_target_to_project`` helper, so ``carve init``
(which creates ``dev``) and this command produce byte-identical artifacts.

P1.1-01 dropped the per-target filesystem tree: this command no longer
creates ``targets/<name>/`` — EL artifacts live in the flat ``el/<name>/``
tree, target-agnostic.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from carve.core.targets.registry import (
    InvalidTargetNameError,
    TargetExistsError,
    add_target_to_project,
)

console = Console()


def command(
    name: str = typer.Argument(..., help="Target name (e.g. staging, prod, eu_prod)."),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing [snowflake.<name>] section.",
    ),
) -> None:
    """Create a new target."""
    root = project_dir.resolve()

    try:
        add_target_to_project(name, root, force=force)
    except InvalidTargetNameError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except TargetExistsError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        console.print("Use [bold]--force[/bold] to overwrite the existing section.")
        raise typer.Exit(code=2) from exc

    upper = name.upper()
    console.print(f'[green]Created target "{name}".[/green]')
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print(f"  1. Add {upper}_* values to .env (see .env.example for the list)")
    console.print(
        f"  2. Review the [snowflake.{name}] section in carve/connections.toml"
    )
    console.print(f"  3. Run a build against this target: carve build <plan_id> --target {name}")
    raise typer.Exit(code=0)
