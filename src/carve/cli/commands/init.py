"""`carve init` — scaffold a Carve project on the control-plane model.

Thin orchestrator over :mod:`carve.init`: **detect** the directory (brownfield
dbt/dlt, git, docker), **resolve** the four orthogonal axes (postgres, dbt,
dlt, memory) into an ``InitPlan``, then **scaffold** the files idempotently.

Lean first pass (see DELIVERY): detection + the control-plane ``carve.toml``
scaffold (simple-mode + separate-component blocks) + non-interactive
resolution + the bundled/external Postgres paths. Deferred: convention
inference, interactive prompts, ``--migrate-from-targets``, auth-token
bootstrap, and the getting-started docs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer
from rich.console import Console
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from carve.cli.commands.packaging import (
    InvalidPostgresUrlError,
    docker_compose_available,
    normalize_postgres_url,
)
from carve.core.config import ServerConfig
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig
from carve.core.state.database import create_engine_from_config, initialize_database
from carve.core.targets.registry import (
    InvalidTargetNameError,
    TargetExistsError,
    add_target_to_project,
    validate_target_name,
)
from carve.init import InitError, InitOptions, detect, resolve, scaffold

console = Console()


def command(
    directory: Path = typer.Argument(
        Path("."),
        help="Directory to initialize. Defaults to the current directory.",
    ),
    external_postgres: str | None = typer.Option(
        None,
        "--external-postgres",
        help="Use an existing Postgres (postgresql+psycopg://…) instead of the "
        "bundled docker-compose Postgres. Skips the compose scaffolding.",
    ),
    with_dbt: bool = typer.Option(
        False, "--with-dbt", help="Scaffold a new dbt project at the root."
    ),
    dbt_path: str | None = typer.Option(
        None, "--dbt-path", help="Use an existing dbt project at this path."
    ),
    dbt_url: str | None = typer.Option(
        None, "--dbt-url", help="Use an existing dbt repo at this git URL."
    ),
    dbt_branch: str = typer.Option("main", "--dbt-branch", help="Branch for --dbt-url."),
    with_dlt: bool = typer.Option(
        False, "--with-dlt", help="Scaffold a sample dlt source at el/sample/."
    ),
    dlt_path: str | None = typer.Option(
        None, "--dlt-path", help="Use an existing dlt project at this path."
    ),
    dlt_url: str | None = typer.Option(
        None, "--dlt-url", help="Use an existing dlt repo at this git URL."
    ),
    dlt_branch: str = typer.Option("main", "--dlt-branch", help="Branch for --dlt-url."),
    project_name: str | None = typer.Option(
        None, "--project-name", help="Override the project name."
    ),
    default_target: str = typer.Option(
        "dev", "--default-target", help="Name of the default target."
    ),
    destination_kind: str = typer.Option(
        "snowflake", "--destination-kind", help="Destination type for the default target."
    ),
    no_git_init: bool = typer.Option(False, "--no-git-init", help="Don't run git init."),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Disable prompts; fail if required input is missing."
    ),
) -> None:
    """Create a Carve project skeleton in `directory`."""
    del non_interactive  # prompts are deferred; resolution is always non-interactive
    root = directory.resolve()
    if not root.name:
        console.print(
            f"[red]Error:[/red] {root} has no directory name component; "
            "refusing to initialize a project at the filesystem root."
        )
        raise typer.Exit(code=2)

    # Validate the target name before writing anything: add_target_to_project
    # runs after the scaffold, so an invalid name would otherwise crash with a
    # traceback and leave a half-written project behind.
    try:
        validate_target_name(default_target)
    except InvalidTargetNameError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    root.mkdir(parents=True, exist_ok=True)

    # Postgres axis: validate an external URL, else the bundled path needs Docker.
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

    detection = detect(root)
    opts = InitOptions(
        project_name=project_name,
        default_target=default_target,
        destination_kind=destination_kind,
        external_postgres_url=database_url,
        with_dbt=with_dbt,
        dbt_path=dbt_path,
        dbt_url=dbt_url,
        dbt_branch=dbt_branch,
        with_dlt=with_dlt,
        dlt_path=dlt_path,
        dlt_url=dlt_url,
        dlt_branch=dlt_branch,
        no_git_init=no_git_init,
    )
    try:
        plan = resolve(detection, opts)
    except InitError as exc:
        console.print(f"[red]Error:[/red] {exc.message}")
        if exc.hint:
            console.print(f"  {exc.hint}")
        raise typer.Exit(code=2) from exc

    console.print(f"[bold]Initializing Carve project in[/bold] {root}")
    if plan.re_init:
        console.print("[yellow]![/yellow] Existing carve.toml — re-init (existing files kept).")

    result = scaffold(root, plan)
    for path in result.dirs_created:
        console.print(f"[green]+[/green] {path}/")
    for path in result.written:
        console.print(f"[green]+[/green] {path}")
    for path in result.kept:
        console.print(f"[yellow]![/yellow] {path} already exists, skipping")

    if database_url is not None:
        console.print(
            "[yellow]![/yellow] External Postgres: bundled docker-compose not "
            "generated. Carve will not manage your Postgres lifecycle "
            "(backups, upgrades, tuning are yours)."
        )
        console.print("  Add this line to your .env (gitignored):")
        console.print(f"    DATABASE_URL={database_url}", markup=False, soft_wrap=True)

    # Connection config for the default target ([<kind>.<target>] in
    # connections.toml + a `# === <target> target ===` .env.example block).
    try:
        add_target_to_project(plan.default_target, root)
        console.print(f"[green]+[/green] {root / 'carve' / 'connections.toml'}")
    except TargetExistsError:
        console.print(
            f"[yellow]![/yellow] {root / 'carve' / 'connections.toml'} "
            f"already has [snowflake.{plan.default_target}], skipping"
        )

    _initialize_state_store(root, database_url=database_url)

    if plan.git_init:
        _git_init(root)

    console.print("[green]✓[/green] Project initialized.")
    console.print("")
    console.print("Next steps:")
    if database_url is None:
        console.print("  docker compose up -d   # start the bundled Postgres")
    console.print("  carve serve            # API + scheduler + worker")
    console.print('  carve plan "ingest the Hacker News top stories"')
    raise typer.Exit(code=0)


def _git_init(root: Path) -> None:
    """Run `git init` in a fresh project (no initial commit). Best-effort."""
    try:
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, text=True)
        console.print("[green]+[/green] initialized empty git repository")
    except (OSError, subprocess.CalledProcessError):
        console.print("[yellow]![/yellow] git init skipped (git not available)")


def _initialize_state_store(project_root: Path, *, database_url: str | None) -> None:
    """Bring the state-store schema to head when Postgres is reachable.

    External Postgres (``database_url`` set) is already running, so it must be
    reachable and migratable — a failure is fatal (exit 3); the engine is built
    from the provided URL directly so a stray ``DATABASE_URL`` env can't
    override it. Bundled Postgres usually isn't up at init time (the compose
    file was only just rendered), so an unreachable database is a next-step,
    not an error — the schema is brought to head later by `carve serve`.
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
        console.print("[yellow]![/yellow] Postgres isn't running yet — schema not initialized.")
        console.print(
            "  Next: `docker compose up -d`, then `carve serve` brings the schema to head."
        )
        return
    console.print("[green]+[/green] state store schema initialized (postgres)")


def _first_error_line(exc: Exception) -> str:
    """A short, single-line summary of a DB error (no multi-line SQL traceback)."""
    orig = getattr(exc, "orig", None)
    text = str(orig) if orig is not None else str(exc)
    first = text.strip().splitlines()[0] if text.strip() else exc.__class__.__name__
    return first[:200]
