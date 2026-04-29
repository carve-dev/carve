"""`carve init` — create the minimum Carve project layout in the current directory.

This is one of the few commands that does real work in M1-01. The exact tree
written here is consumed by `M1-02` (config loader) and several later specs,
so the contents are intentionally fixed rather than configurable.
"""

from pathlib import Path

import typer
from rich.console import Console

console = Console()

CARVE_TOML_CONTENT = """\
[project]
name = "my-carve-project"
version = "0.0.1"
default_target = "dev"

[paths]
config_dir = "carve"
"""

CONNECTIONS_TOML_CONTENT = """\
# Connection definitions live here. See `M1-02` for the schema.
"""

RUNNER_TOML_CONTENT = """\
# Runner configuration lives here. See `M1-02` for the schema.
"""

ENV_EXAMPLE_CONTENT = """\
# Copy this to `.env` and fill in real values. `.env` is gitignored.
# ANTHROPIC_API_KEY=
"""

GITIGNORE_CONTENT = """\
.env
.carve/
*.sqlite
*.sqlite3
"""


def _write_if_missing(path: Path, content: str) -> bool:
    """Write `content` to `path` if it does not already exist.

    Returns True when the file was written, False when it was skipped.
    """
    if path.exists():
        console.print(f"[yellow]![/yellow] {path} already exists, skipping")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    console.print(f"[green]+[/green] {path}")
    return True


def _ensure_dir(path: Path) -> None:
    if path.exists():
        console.print(f"[yellow]![/yellow] {path}/ already exists, skipping")
        return
    path.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]+[/green] {path}/")


def command(
    directory: Path = typer.Argument(
        Path("."),
        help="Directory to initialize. Defaults to the current directory.",
    ),
) -> None:
    """Create a new Carve project skeleton in `directory`."""
    root = directory.resolve()
    root.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Initializing Carve project in[/bold] {root}")

    _write_if_missing(root / "carve.toml", CARVE_TOML_CONTENT)
    _write_if_missing(root / "carve" / "connections.toml", CONNECTIONS_TOML_CONTENT)
    _write_if_missing(root / "carve" / "runner.toml", RUNNER_TOML_CONTENT)
    _ensure_dir(root / "carve" / "agents")
    _ensure_dir(root / "pipelines")
    _write_if_missing(root / ".env.example", ENV_EXAMPLE_CONTENT)
    _write_if_missing(root / ".gitignore", GITIGNORE_CONTENT)

    console.print("[green]✓[/green] Project initialized.")
    raise typer.Exit(code=0)
