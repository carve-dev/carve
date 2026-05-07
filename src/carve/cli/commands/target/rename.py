"""``carve target rename`` — rename a target across all locations."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import tomlkit
import typer
from rich.console import Console

from carve.core.targets.registry import (
    InvalidTargetNameError,
    TargetExistsError,
    TargetNotFoundError,
    list_target_sections,
    rename_env_example_block,
    rename_target_section,
    validate_target_name,
)

console = Console()


def command(
    old: str = typer.Argument(..., help="Existing target name."),
    new: str = typer.Argument(..., help="New target name."),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Rename ``<old>`` to ``<new>`` across connections.toml, .env.example,
    targets/, and (if applicable) carve.toml's ``default_target``."""
    root = project_dir.resolve()

    try:
        validate_target_name(old)
        validate_target_name(new)
    except InvalidTargetNameError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    conn_path = root / "carve" / "connections.toml"
    env_example_path = root / ".env.example"
    targets_root = root / "targets"
    old_dir = targets_root / old
    new_dir = targets_root / new
    carve_toml = root / "carve.toml"

    existing = list_target_sections(conn_path)
    if old not in existing:
        console.print(
            f'[red]Error:[/red] target "{old}" not defined in {conn_path}.'
        )
        raise typer.Exit(code=2)
    if new in existing:
        console.print(
            f'[red]Error:[/red] target "{new}" already exists in {conn_path}.'
        )
        raise typer.Exit(code=2)
    if new_dir.exists():
        console.print(
            f'[red]Error:[/red] {new_dir} already exists; refusing to overwrite.'
        )
        raise typer.Exit(code=2)

    # 1) Rename the section in connections.toml.
    try:
        rename_target_section(old, new, conn_path)
    except (TargetNotFoundError, TargetExistsError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    # 2) Rewrite the .env.example block.
    rename_env_example_block(old, new, env_example_path)

    # 3) Move the artifacts directory if it exists.
    moved_dir = False
    if old_dir.exists():
        if (root / ".git").is_dir():
            try:
                subprocess.run(
                    ["git", "mv", str(old_dir), str(new_dir)],
                    cwd=str(root),
                    check=True,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Fall back to plain mv if git mv fails (e.g. file not tracked).
                shutil.move(str(old_dir), str(new_dir))
        else:
            shutil.move(str(old_dir), str(new_dir))
        moved_dir = True

    # 4) Update default_target in carve.toml if applicable.
    updated_default = False
    if carve_toml.is_file():
        text = carve_toml.read_text(encoding="utf-8")
        doc = tomlkit.parse(text)
        project = doc.get("project")
        if isinstance(project, dict) and project.get("default_target") == old:
            project["default_target"] = new
            carve_toml.write_text(tomlkit.dumps(doc), encoding="utf-8")
            updated_default = True

    console.print(f'[green]Renamed target "{old}" → "{new}".[/green]')
    console.print(f"  - connections.toml: [snowflake.{old}] → [snowflake.{new}]")
    if env_example_path.is_file():
        console.print(f"  - .env.example: {old.upper()}_* → {new.upper()}_*")
    if moved_dir:
        console.print(f"  - targets/{old}/ → targets/{new}/")
    if updated_default:
        console.print(f'  - carve.toml: default_target = "{new}"')
    console.print(
        f"\n[yellow]Reminder:[/yellow] update {old.upper()}_* env vars in your "
        f"local .env to {new.upper()}_* (Carve does not edit .env)."
    )
    raise typer.Exit(code=0)
