"""``carve skills`` — list / show / test the skill catalog.

Surfaces every skill provider in one place:

* **built-in** callable ``@skill`` functions (the catalog registry),
* **pack** skill packs (folder ``SKILL.md``, content-injected),
* **mcp** imported MCP tools (namespaced ``mcp:<server>:<tool>``).
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

import carve.core.skills.builtin  # noqa: F401  (registers built-in skills)
from carve.core.config import ConfigError, load_config
from carve.core.mcp.client import import_server_tools
from carve.core.mcp.config import load_mcp_config
from carve.core.skills.builtin import DEFERRED_READER_SKILLS
from carve.core.skills.decorator import SkillFn, get_metadata
from carve.core.skills.pack_discovery import discover_pack_roots
from carve.core.skills.registry import default_registry

app = typer.Typer(
    name="skills",
    help="List, show, and test skills (built-in, packs, MCP).",
    no_args_is_help=True,
)

console = Console()


def _skills_dir(root: Path) -> Path:
    skills_dir = "carve/skills"
    try:
        skills_dir = load_config(root).paths.skills_dir
    except ConfigError:
        pass
    return (root / skills_dir).resolve()


def _mcp_file(root: Path) -> Path:
    mcp_file = "carve/mcp.toml"
    try:
        mcp_file = load_config(root).paths.mcp_file
    except ConfigError:
        pass
    return (root / mcp_file).resolve()


@app.command(name="list")
def list_skills(
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """List every skill across providers, with the provider of each."""
    root = project_dir.resolve()
    table = Table(title="Skills", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Provider")
    table.add_column("Description")

    for name in sorted(default_registry().names()):
        fn = default_registry()[name]
        table.add_row(name, "built-in", _skill_description(fn))

    for pack in discover_pack_roots(skills_dir=_skills_dir(root)).discover():
        table.add_row(pack.name, "pack", pack.description)

    servers = load_mcp_config(_mcp_file(root)).server
    for server in servers:
        for imported in import_server_tools(server):
            writes = "writes" if imported.writes else "read-only"
            table.add_row(imported.name, "mcp", f"effects={list(imported.effects)} ({writes})")

    for name, owner in sorted(DEFERRED_READER_SKILLS.items()):
        table.add_row(name, "built-in (deferred)", owner)

    console.print(table)
    raise typer.Exit(code=0)


@app.command(name="show")
def show_skill(
    name: str = typer.Argument(..., help="The skill or pack name to show."),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Show one skill's description (built-in) or instructions (pack)."""
    root = project_dir.resolve()
    if name in default_registry():
        console.print(f"[bold]{name}[/bold] (built-in)")
        console.print(_skill_description(default_registry()[name]))
        raise typer.Exit(code=0)

    for pack in discover_pack_roots(skills_dir=_skills_dir(root)).discover():
        if pack.name == name:
            console.print(f"[bold]{pack.name}[/bold] (pack)")
            console.print(f"  description : {pack.description}")
            console.print(f"  expects_env : {list(pack.expects_env)}")
            console.print("\n[dim]--- instructions ---[/dim]")
            console.print(pack.instructions)
            raise typer.Exit(code=0)

    console.print(f"[red]No skill or pack named {name!r}.[/red]")
    raise typer.Exit(code=1)


@app.command(name="test")
def test_skill(
    name: str = typer.Argument(..., help="The skill pack to test-load."),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Test-load a skill pack: render exactly what would be injected.

    Loading is side-effect-free (no bundled script runs); this surfaces the
    content + the inert bundle pointers so an author can verify a pack
    before relying on it.
    """
    root = project_dir.resolve()
    library = discover_pack_roots(skills_dir=_skills_dir(root))
    by_name = {p.name: p for p in library.discover()}
    if name not in by_name:
        console.print(f"[red]No skill pack named {name!r}.[/red] Available: {sorted(by_name)}")
        raise typer.Exit(code=1)
    pack = by_name[name]
    console.print(f"[green]Pack {name!r} loaded OK[/green] from {pack.directory}")
    if pack.script_paths:
        console.print(f"  bundled scripts (inert): {[str(p) for p in pack.script_paths]}")
    console.print("\n[dim]--- injected content ---[/dim]")
    console.print(pack.instructions)
    raise typer.Exit(code=0)


def _skill_description(fn: SkillFn) -> str:
    return get_metadata(fn).description
