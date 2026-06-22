"""`carve init` — create the minimum Carve project layout in the current directory.

The exact tree written here is consumed by `M1-02` (config loader) and several
later specs, so the contents are intentionally fixed rather than configurable.

P1.1-01 dropped the per-target ``targets/<X>/el/`` scaffolding. ``carve init``
now creates an empty ``el/`` tree (artifacts land there directly,
target-agnostic) and delegates connection-section / env-example-block
scaffolding to ``add_target_to_project("dev", root)``. The target abstraction
survives — ``[snowflake.<name>]`` sections in ``connections.toml`` and
``<NAME>_*`` env-var prefixes — but nothing lives under ``targets/`` anymore.
"""

import json
from pathlib import Path

import typer
from rich.console import Console
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from carve.cli.commands.packaging import (
    InvalidPostgresUrlError,
    bundled_env_block,
    docker_compose_available,
    external_env_block,
    normalize_postgres_url,
    render_compose,
)
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

# Recovery agent (P1-09). Set `enabled = false` or pass --no-auto-fix on
# the CLI to disable the auto-fix loop. `max_attempts` is the per-failure
# budget — deploy phases each get their own pool.
# [auto_fix]
# enabled = true
# max_attempts = 3
"""

MODELS_TOML_CONTENT = """\
# Anthropic / model configuration. The keys here populate the `models`
# section of the merged config — write fields at the top level, no header.

# How Carve authenticates to Anthropic. Leave `auth_mode` unset to
# auto-resolve (API key first, then a Claude-subscription OAuth token), or
# pin it explicitly:
#   auth_mode = "api_key"   # uses ANTHROPIC_API_KEY
#   auth_mode = "oauth"     # uses a Claude-subscription OAuth token
#                           # (ANTHROPIC_AUTH_TOKEN / CLAUDE_CODE_OAUTH_TOKEN;
#                           #  mint one with `carve auth login`)

# anthropic_api_key = "${ANTHROPIC_API_KEY}"
# default_model = "claude-opus-4-8"

# Optional named model tiers a per-agent `model:` may reference:
# [tiers]
# fast = "claude-haiku-4-5"
"""

ENV_EXAMPLE_HEADER = """\
# Copy this to `.env` and fill in real values. `.env` is gitignored.

# === Project-wide ===
# Model-provider credential — set ONE of these (not both; the API rejects
# requests carrying both). A developer-portal API key:
ANTHROPIC_API_KEY=
# …or a Claude-subscription OAuth token (mint with `carve auth login`):
# ANTHROPIC_AUTH_TOKEN=
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
    external_postgres: str | None = typer.Option(
        None,
        "--external-postgres",
        help=(
            "Use an existing Postgres (postgresql+psycopg://…) instead of the "
            "bundled docker-compose Postgres. Skips the compose scaffolding."
        ),
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

    # Resolve the state-store mode up front. external_postgres -> validate the
    # URL and skip the compose bundle; otherwise the bundled path needs Docker.
    database_url: str | None = None
    if external_postgres is not None:
        try:
            database_url = normalize_postgres_url(external_postgres)
        except InvalidPostgresUrlError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=2) from exc
    elif not docker_compose_available():
        console.print(
            "[red]Error:[/red] Docker not detected. Either install Docker "
            "(Docker Desktop / `docker compose`), or re-run with "
            '`--external-postgres "postgresql+psycopg://…"` pointing at your '
            "existing Postgres."
        )
        raise typer.Exit(code=3)

    console.print(f"[bold]Initializing Carve project in[/bold] {root}")

    _write_if_missing(root / "carve.toml", _carve_toml_content(root.name))
    _write_if_missing(root / "carve" / "runner.toml", RUNNER_TOML_CONTENT)
    _write_if_missing(root / "carve" / "models.toml", MODELS_TOML_CONTENT)
    _ensure_dir(root / "carve" / "agents")
    _ensure_dir(root / "el")
    state_block = external_env_block() if database_url is not None else bundled_env_block()
    _write_if_missing(root / ".env.example", ENV_EXAMPLE_HEADER + state_block)
    _write_if_missing(root / ".gitignore", GITIGNORE_CONTENT)

    # Bundled path: drop the Postgres-only docker-compose template (left alone
    # on re-run if it already exists). External path: no compose; print the
    # lifecycle boundary + the real DATABASE_URL for the user to paste into
    # their gitignored .env (never written to the committed .env.example).
    if database_url is None:
        _write_if_missing(root / "docker-compose.yml", render_compose(root.name))
    else:
        console.print(
            "[yellow]![/yellow] External Postgres: bundled docker-compose not "
            "generated. Carve will not manage your Postgres lifecycle "
            "(backups, upgrades, tuning are yours)."
        )
        console.print("  Add this line to your .env (gitignored):")
        # soft_wrap so the (possibly long) URL stays on one copyable line.
        console.print(f"    DATABASE_URL={database_url}", markup=False, soft_wrap=True)

    # Add the default "dev" target — creates the [snowflake.dev]
    # section in carve/connections.toml and appends the
    # `# === dev target ===` block to .env.example. Since P1.1-01
    # the helper no longer creates `targets/dev/el/`; artifacts live
    # directly under `el/` (created above). The target abstraction
    # is now purely connection config.
    try:
        add_target_to_project("dev", root)
        console.print(f"[green]+[/green] {root / 'carve' / 'connections.toml'}")
    except TargetExistsError:
        console.print(
            f"[yellow]![/yellow] {root / 'carve' / 'connections.toml'} "
            "already has [snowflake.dev], skipping"
        )

    _initialize_state_store(root, database_url=database_url)

    console.print("[green]✓[/green] Project initialized.")
    raise typer.Exit(code=0)


def _initialize_state_store(project_root: Path, *, database_url: str | None) -> None:
    """Bring the state-store schema to head when Postgres is reachable.

    `carve init` runs before `models.toml` exists, so we can't `load_config()`
    here — we synthesise a minimal Config the state-store helpers will read.

    Migration timing differs by path:

    * **External Postgres** (`database_url` set) is already running, so it must
      be reachable and migratable — a failure is fatal (exit 3). The engine is
      built from the provided URL **directly** (not via
      `resolve_state_store_url`) so a stray `DATABASE_URL` env can't override
      it — even if the external URL happens to equal the dev default. Running
      the migrations also exercises the connecting user's CREATE TABLE right.
    * **Bundled Postgres** (`database_url is None`) usually isn't up yet at
      init time (the compose file was only just rendered). An unreachable
      database is therefore a next-step, not an error: the schema is brought
      to head later, once the user runs `docker compose up -d` + `carve serve`.
    """
    try:
        if database_url is not None:
            engine = create_engine(database_url, future=True, pool_pre_ping=True)
        else:
            config = Config(
                project=ProjectConfig(name="bootstrap"),
                models=ModelsConfig(anthropic_api_key="bootstrap"),
                server=ServerConfig(),
            )
            engine = create_engine_from_config(config, project_dir=project_root)
        initialize_database(engine)
        engine.dispose()
    except (OperationalError, ProgrammingError) as exc:
        if database_url is not None:
            console.print("[red]Error:[/red] couldn't initialize the external Postgres.")
            console.print(f"  {_first_error_line(exc)}")
            console.print(
                "  Check the URL is reachable and the connecting user can "
                "CREATE TABLE (Carve runs migrations)."
            )
            raise typer.Exit(code=3) from exc
        console.print(
            "[yellow]![/yellow] Postgres isn't running yet — schema not "
            "initialized."
        )
        console.print(
            "  Next: `docker compose up -d`, then `carve serve` brings the "
            "schema to head."
        )
        return
    console.print("[green]+[/green] state store schema initialized (postgres)")


def _first_error_line(exc: Exception) -> str:
    """A short, single-line summary of a DB error (no multi-line SQL traceback)."""
    orig = getattr(exc, "orig", None)
    text = str(orig) if orig is not None else str(exc)
    first = text.strip().splitlines()[0] if text.strip() else exc.__class__.__name__
    return first[:200]
