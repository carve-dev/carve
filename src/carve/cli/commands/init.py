"""`carve init` — create the minimum Carve project layout in the current directory.

The exact tree written here is consumed by `M1-02` (config loader) and several
later specs, so the contents are intentionally fixed rather than configurable.

P1-01 refactored this command to delegate the connections-section /
env-example-block / per-target artifact-dir scaffolding to
``add_target_to_project("dev", root)``. The single helper guarantees that
``carve init`` and ``carve target create`` produce byte-identical artifacts
for the parts they share.
"""

import json
from pathlib import Path

import typer
from rich.console import Console

from carve.core.config import ServerConfig
from carve.core.config.schema import (
    Config,
    ModelsConfig,
    ProjectConfig,
)
from carve.core.state.database import (
    create_engine_from_config,
    initialize_database,
)
from carve.core.targets.registry import (
    TargetExistsError,
    add_target_to_project,
)

console = Console()

_CARVE_TOML_TEMPLATE = """\
[project]
name = {name}
version = "0.0.1"
default_target = "dev"

[paths]
config_dir = "carve"
agents_dir = "carve/agents"
targets_dir = "targets"
"""


def _carve_toml_content(project_name: str) -> str:
    """Return the rendered ``carve.toml`` body for ``project_name``.

    The project name is detected from the project root's directory name at
    init time (``Path(directory).resolve().name``); users can edit
    ``carve.toml`` after the fact if they want a different display name.

    The name is escaped via ``json.dumps`` — TOML basic strings share their
    escape grammar with JSON strings (same ``\\n`` / ``\\"`` / ``\\\\`` /
    ``\\uXXXX``), so a directory whose name contains quotes, newlines, or
    other meta-characters renders as a single, valid TOML key rather than
    breaking the file or injecting bonus tables.
    """
    return _CARVE_TOML_TEMPLATE.format(name=json.dumps(project_name))


RUNNER_TOML_CONTENT = """\
# Runner configuration. The keys here populate the `runner` section of
# the merged config — write fields at the top level, no header.
# The `local_venv` runner is the only M1 option; Docker / remote runners
# arrive later.

# type = "local_venv"
# venv_cache_dir = ".carve/venvs"
# default_timeout_seconds = 1800
# max_concurrent_runs = 4
"""

MODELS_TOML_CONTENT = """\
# Anthropic / model configuration. The keys here populate the `models`
# section of the merged config — write fields at the top level, no header.

# anthropic_api_key = "${ANTHROPIC_API_KEY}"
# default_model = "claude-sonnet-4-5-20250929"

# To use your Claude Code subscription instead of an API key, see M1.1-02
# (auth_mode = "claude_code_oauth"). Not yet implemented as of this version.
"""

ENV_EXAMPLE_HEADER = """\
# Copy this to `.env` and fill in real values. `.env` is gitignored.

# === Project-wide ===
ANTHROPIC_API_KEY=
# GITHUB_TOKEN=                          # uncomment if using `carve el deploy`
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
    if not root.name:
        console.print(
            f"[red]Error:[/red] {root} has no directory name component; "
            "refusing to initialize a project at the filesystem root."
        )
        raise typer.Exit(code=2)
    root.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Initializing Carve project in[/bold] {root}")

    _write_if_missing(root / "carve.toml", _carve_toml_content(root.name))
    _write_if_missing(root / "carve" / "runner.toml", RUNNER_TOML_CONTENT)
    _write_if_missing(root / "carve" / "models.toml", MODELS_TOML_CONTENT)
    _ensure_dir(root / "carve" / "agents")
    _write_if_missing(root / ".env.example", ENV_EXAMPLE_HEADER)
    _write_if_missing(root / ".gitignore", GITIGNORE_CONTENT)

    # Add the default "dev" target — creates carve/connections.toml's
    # [snowflake.dev] section, appends the # === dev target === block to
    # .env.example, and creates targets/dev/el/. The same helper backs
    # `carve target create`, so init's output for these three artifacts
    # is byte-identical to what a user would get by running
    # `carve target create dev` in a fresh repo. Init is idempotent: a
    # re-run on an already-initialised project leaves the existing
    # ``[snowflake.dev]`` section untouched.
    try:
        add_target_to_project("dev", root)
        console.print(f"[green]+[/green] {root / 'carve' / 'connections.toml'}")
        console.print(f"[green]+[/green] {root / 'targets' / 'dev' / 'el'}/")
    except TargetExistsError:
        console.print(
            f"[yellow]![/yellow] {root / 'carve' / 'connections.toml'} "
            "already has [snowflake.dev], skipping"
        )
        # Still ensure the artifact dir exists (cheap, idempotent).
        (root / "targets" / "dev" / "el").mkdir(parents=True, exist_ok=True)

    _initialize_state_store(root)

    console.print("[green]✓[/green] Project initialized.")
    raise typer.Exit(code=0)


def _initialize_state_store(project_root: Path) -> None:
    """Create `.carve/state.db` with the M1 schema.

    `carve init` runs before `models.toml` exists, so we can't call
    `load_config()` here. Instead we synthesise a minimal Config that
    only the state-store helpers will read — they touch
    `config.server.state_store` and nothing else.
    """
    config = Config(
        project=ProjectConfig(name="bootstrap"),
        models=ModelsConfig(anthropic_api_key="bootstrap"),
        server=ServerConfig(),
    )
    engine = create_engine_from_config(config, project_dir=project_root)
    initialize_database(engine)
    engine.dispose()
    console.print(f"[green]+[/green] {project_root / '.carve' / 'state.db'}")
