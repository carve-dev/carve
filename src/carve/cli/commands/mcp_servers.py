"""``carve mcp-servers`` — register / list / remove external MCP servers."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from carve.core.config import ConfigError, load_config
from carve.core.mcp.client import import_server_tools
from carve.core.mcp.config import (
    McpConfigError,
    add_server,
    load_mcp_config,
    remove_server,
)

app = typer.Typer(
    name="mcp-servers",
    help="Register, list, and remove external MCP servers (consume).",
    no_args_is_help=True,
)

console = Console()


def _mcp_file(root: Path) -> Path:
    mcp_file = "carve/mcp.toml"
    try:
        mcp_file = load_config(root).paths.mcp_file
    except ConfigError:
        pass
    return (root / mcp_file).resolve()


@app.command(name="add")
def add(
    name: str = typer.Argument(..., help="A name for the server (the namespace)."),
    command: str | None = typer.Option(
        None, "--command", help="The stdio command that launches the server."
    ),
    url: str | None = typer.Option(
        None, "--url", help="A remote server URL (use instead of --command)."
    ),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Register an MCP server in carve/mcp.toml."""
    root = project_dir.resolve()
    try:
        add_server(_mcp_file(root), name=name, command=command, url=url)
    except McpConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Registered MCP server {name!r}.[/green]")
    raise typer.Exit(code=0)


@app.command(name="list")
def list_servers(
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """List registered MCP servers and their imported (tagged) tools."""
    root = project_dir.resolve()
    try:
        config = load_mcp_config(_mcp_file(root))
    except McpConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if not config.server:
        console.print(
            "[yellow]No MCP servers registered.[/yellow] "
            "Add one with `carve mcp-servers add <name> --command ...`."
        )
        raise typer.Exit(code=0)

    table = Table(title="MCP servers", show_lines=True)
    table.add_column("Server", style="bold")
    table.add_column("Endpoint")
    table.add_column("Imported tools")
    for server in config.server:
        endpoint = server.command or server.url or "?"
        tools = import_server_tools(server)
        rendered = "\n".join(
            f"{t.name}  [{'writes' if t.writes else 'read-only'}]" for t in tools
        ) or "(none declared)"
        table.add_row(server.name, endpoint, rendered)
    console.print(table)
    raise typer.Exit(code=0)


@app.command(name="remove")
def remove(
    name: str = typer.Argument(..., help="The server name to remove."),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Remove a registered MCP server from carve/mcp.toml."""
    root = project_dir.resolve()
    removed = remove_server(_mcp_file(root), name=name)
    if not removed:
        console.print(f"[yellow]No MCP server named {name!r}.[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"[green]Removed MCP server {name!r}.[/green]")
    raise typer.Exit(code=0)
