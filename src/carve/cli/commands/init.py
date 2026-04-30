"""`carve init` — create the minimum Carve project layout in the current directory.

This is one of the few commands that does real work in M1-01. The exact tree
written here is consumed by `M1-02` (config loader) and several later specs,
so the contents are intentionally fixed rather than configurable.
"""

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
# Connection definitions for Snowflake (and future connectors).
# The key after `[snowflake.<target>]` is the target name, referenced from
# carve.toml's `default_target` (default: "dev").
#
# Use ${VAR_NAME} to interpolate environment variables from .env or your shell.

# [snowflake.dev]
# account = "${SNOWFLAKE_ACCOUNT}"          # e.g. "abc12345.us-east-1"
# user = "${SNOWFLAKE_USER}"
# password = "${SNOWFLAKE_PASSWORD}"
# role = "${SNOWFLAKE_ROLE}"                # e.g. "SYSADMIN"
# warehouse = "${SNOWFLAKE_WAREHOUSE}"      # e.g. "COMPUTE_WH"
# database = "${SNOWFLAKE_DATABASE}"
# schema = "PUBLIC"                          # optional; defaults to PUBLIC

# Alternative auth methods (uncomment one and remove `password = ...`):
#
# Key-pair:
#   private_key_path = "/path/to/rsa_key.p8"
#   # set SNOWFLAKE_PRIVATE_KEY_PASSPHRASE in your env if the key is encrypted
#
# SSO / external browser (dev only — pops a browser window):
#   authenticator = "externalbrowser"
"""

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

ENV_EXAMPLE_CONTENT = """\
# Copy this to `.env` and fill in real values. `.env` is gitignored.
# ANTHROPIC_API_KEY=

# Snowflake (used by carve/connections.toml's [snowflake.dev]):
# SNOWFLAKE_ACCOUNT=
# SNOWFLAKE_USER=
# SNOWFLAKE_PASSWORD=
# SNOWFLAKE_ROLE=
# SNOWFLAKE_WAREHOUSE=
# SNOWFLAKE_DATABASE=
# SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=
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
    _write_if_missing(root / "carve" / "models.toml", MODELS_TOML_CONTENT)
    _ensure_dir(root / "carve" / "agents")
    _ensure_dir(root / "pipelines")
    _write_if_missing(root / ".env.example", ENV_EXAMPLE_CONTENT)
    _write_if_missing(root / ".gitignore", GITIGNORE_CONTENT)

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
