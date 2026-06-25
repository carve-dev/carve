"""`carve build` — materialise a draft plan into pipeline files."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape as _escape

from carve.cli.orchestrator import build_plan
from carve.cli.orchestrator.builder import (
    BuildError,
    ConfigDriftError,
    PlanExpiredError,
)
from carve.cli.orchestrator.observers import RichConsoleObserver
from carve.core.agents.exceptions import AgentError
from carve.core.config import ConfigError, load_config
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

console = Console()


def command(
    plan_id: str = typer.Argument(..., help="ID of the draft plan to build."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Rebuild even if the plan is already in phase=built.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress live progress output; print only the final summary.",
    ),
    table: str | None = typer.Option(
        None,
        "--table",
        help=(
            "Override the destination table at build time. The plan's "
            "design.destination.table is replaced before the agent runs."
        ),
    ),
    database: str | None = typer.Option(
        None,
        "--database",
        help=(
            "Override the destination database. Lands in destination.toml "
            "as a live override; without this, the runtime database "
            "inherits from <TARGET>_SNOWFLAKE_DATABASE."
        ),
    ),
    schema_: str | None = typer.Option(
        None,
        "--schema",
        help=(
            "Override the destination schema. Same shape as --database — "
            "lands as an override in destination.toml; otherwise inherits "
            "from <TARGET>_SNOWFLAKE_SCHEMA."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=(
            "Skip the destination-FQN confirmation prompt. Useful for "
            "scripted CI builds; equivalent to answering `y`."
        ),
    ),
) -> None:
    """Run the build agent against ``plan_id`` and write pipeline files."""
    project_dir = Path.cwd()

    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    repository = Repository(session_factory)

    # Confirm or override the destination FQN before invoking the
    # build agent. CLI flags override the plan's design unconditionally;
    # without flags and without --yes, the user gets a y/e/n prompt.
    confirm_result = _confirm_or_override_destination(
        plan_id=plan_id,
        repository=repository,
        config=config,
        cli_table=table,
        cli_database=database,
        cli_schema=schema_,
        skip_prompt=yes,
    )
    if confirm_result is False:  # explicit abort from prompt
        raise typer.Exit(code=1)
    # Narrow: confirm_result is now `None | dict[str, str]`.
    destination_override: dict[str, str] | None = (
        confirm_result if isinstance(confirm_result, dict) else None
    )

    observer = RichConsoleObserver(console, quiet=quiet)

    try:
        try:
            artifact = build_plan(
                plan_id=plan_id,
                config=config,
                project_dir=project_dir,
                repository=repository,
                observer=observer,
                force=force,
                destination_override=destination_override,
            )
        finally:
            observer.close()
    except ConfigDriftError as exc:
        # Drift is its own exit code (3) and its own remediation: re-plan
        # against current config. Caught before the generic BuildError
        # since it subclasses it.
        console.print(f"[red]✗ Config drift:[/red] {exc}")
        console.print(
            "[yellow]Re-plan against current config:[/yellow] "
            f'carve plan --refine {_escape(plan_id)} "<same goal>"  '
            "(or `carve plan` afresh), then build the new plan."
        )
        raise typer.Exit(code=3) from exc
    except PlanExpiredError as exc:
        # Expiry is a plan-state error → the generic exit code 2.
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except BuildError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except AgentError as exc:
        console.print(f"[red]✗ Agent error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    if not artifact.success:
        console.print(
            "[red]✗[/red] Build did not write a `main.py`. Plan stays in "
            "phase=drafted; refine and retry."
        )
        if artifact.summary.strip():
            console.print(artifact.summary.strip())
        raise typer.Exit(code=1)

    # An idempotent no-op carries an empty run id (no build agent ran).
    is_noop = not artifact.run_id
    if is_noop:
        console.print(
            "[green]✓[/green] Plan already built against unchanged config — "
            f"nothing to do (reused build [bold]"
            f"{_escape(artifact.build_id) if artifact.build_id else '(none)'}[/bold])."
        )
    else:
        console.print(
            f"[green]✓[/green] Built pipeline [bold]{_escape(artifact.pipeline_name)}[/bold]"
        )
    console.print(f"  Plan:           {_escape(artifact.plan_id)}")
    console.print(f"  Target:         {_escape(artifact.target)}")
    console.print(
        f"  Build id:       {_escape(artifact.build_id) if artifact.build_id else '(none)'}"
    )
    console.print(f"  Build run id:   {_escape(artifact.run_id) if artifact.run_id else '(no-op)'}")
    console.print("  Files written:")
    for path in artifact.files_written:
        console.print(f"    - {path}")
    console.print(
        f"  Tokens:         {artifact.tokens_input:,} in / {artifact.tokens_output:,} out"
    )
    console.print(f"  Cost:           ${artifact.cost_usd:.4f}")
    if artifact.summary.strip():
        console.print()
        console.print(artifact.summary.strip())
    console.print()
    console.print(f"Next:  carve run {artifact.pipeline_name}")
    console.print(f"       carve el deploy {artifact.pipeline_name} --to {artifact.target}")
    raise typer.Exit(code=0)


def _confirm_or_override_destination(
    *,
    plan_id: str,
    repository: Repository,
    config: object,
    cli_table: str | None,
    cli_database: str | None,
    cli_schema: str | None,
    skip_prompt: bool,
) -> dict[str, str] | None | bool:
    """Surface the destination FQN before the build agent runs.

    Returns one of:

    * ``None`` — no override; build_plan should use the plan's
      stored ``design.destination`` as-is.
    * ``dict`` — override one or more of ``database`` / ``schema`` /
      ``table``. The builder applies these to ``design.destination``
      before invoking the agent.
    * ``False`` — user explicitly aborted the prompt with ``n``.
      Caller should exit non-zero without building.

    The prompt distinguishes "live override" (value differs from the
    target's connection default) from "inherit" (matches default or
    unset).
    """

    from rich.markup import escape as _esc

    from carve.cli.commands.el import resolve_subcommand_target
    from carve.core.targets.resolution import resolve_active_target

    # Read the plan's stored destination so we can show the user what
    # the agent originally proposed.
    plan_row = repository.get_plan(plan_id)
    if plan_row is None or not plan_row.task_graph_json:
        return None  # build_plan will raise its own clean error

    # v0.1-01: task_graph_json is JSONB; ORM returns dict directly.
    raw = plan_row.task_graph_json
    task_graph = raw if isinstance(raw, dict) else None
    if task_graph is None:
        return None
    design = task_graph.get("design")
    plan_destination = design.get("destination") if isinstance(design, dict) else None
    if not isinstance(plan_destination, dict):
        plan_destination = {}

    # Resolve the active target so we can compare against connection
    # defaults for the env-vs-override classification.
    active_target = resolve_active_target(
        resolve_subcommand_target(None),
        config,  # type: ignore[arg-type]
    )
    target_section = config.connections.snowflake.get(active_target)  # type: ignore[attr-defined]
    env_db = target_section.database if target_section is not None else None
    env_schema = target_section.schema_ if target_section is not None else None

    # Apply CLI flags as the proposed values; fall back to plan's.
    proposed = {
        "database": cli_database or plan_destination.get("database"),
        "schema": cli_schema or plan_destination.get("schema"),
        "table": cli_table or plan_destination.get("table"),
    }

    # Build the override dict only for fields that differ from what's
    # already in the plan (so build_plan only mutates what it needs).
    override: dict[str, str] = {}
    if cli_database is not None and cli_database != plan_destination.get("database"):
        override["database"] = cli_database
    if cli_schema is not None and cli_schema != plan_destination.get("schema"):
        override["schema"] = cli_schema
    if cli_table is not None and cli_table != plan_destination.get("table"):
        override["table"] = cli_table

    if skip_prompt:
        return override or None

    # Prompt the user. Print the destination prominently with
    # provenance per field.
    console.print()
    console.print(f"[bold]Destination for target=[cyan]{active_target}[/cyan]:[/bold]")

    def _provenance(field: str, value: str | None, env_value: str | None) -> str:
        if value is None:
            if env_value:
                return f"[dim]<inherits {env_value} from env>[/dim]"
            return "[red]<unset and no env default>[/red]"
        if env_value is None:
            return "[yellow](override; no env default)[/yellow]"
        if value == env_value:
            return "[green](matches env default)[/green]"
        return f"[yellow](override; env default is {_esc(env_value)})[/yellow]"

    console.print(
        f"  database: [bold]{_esc(str(proposed['database'] or '?'))}[/bold]"
        f"  {_provenance('database', proposed['database'], env_db)}"
    )
    console.print(
        f"  schema:   [bold]{_esc(str(proposed['schema'] or '?'))}[/bold]"
        f"  {_provenance('schema', proposed['schema'], env_schema)}"
    )
    console.print(
        f"  table:    [bold]{_esc(str(proposed['table'] or '?'))}[/bold]"
        f"  [dim](always literal)[/dim]"
    )
    console.print()

    while True:
        answer = (
            typer.prompt(
                "Confirm? (y to proceed, e to edit, n to abort)",
                default="y",
                show_default=True,
            )
            .strip()
            .lower()
        )
        if answer in ("y", "yes"):
            return override or None
        if answer in ("n", "no"):
            console.print("[yellow]Aborted by user.[/yellow]")
            return False
        if answer in ("e", "edit"):
            for field in ("database", "schema", "table"):
                current = proposed[field]
                new_value = typer.prompt(
                    f"  {field}",
                    default=str(current) if current is not None else "",
                    show_default=True,
                ).strip()
                if not new_value:
                    # User cleared the value → unset (only meaningful
                    # for database / schema; table is required).
                    if field == "table":
                        console.print("[red]✗ table is required.[/red]")
                        continue
                    proposed[field] = None
                    if plan_destination.get(field) is not None:
                        override[field] = ""  # signal: clear the field
                else:
                    proposed[field] = new_value
                    if new_value != plan_destination.get(field):
                        override[field] = new_value
            console.print("[green]✓[/green] Updated. Confirming...")
            # Re-show the resolved values, then loop to ask y/n/e
            # again. Most users will pick `y` after editing once.
            console.print()
            console.print(f"[bold]Destination for target=[cyan]{active_target}[/cyan]:[/bold]")
            for field, env_value in (
                ("database", env_db),
                ("schema", env_schema),
                ("table", None),
            ):
                console.print(
                    f"  {field:<9} [bold]"
                    f"{_esc(str(proposed[field] or '?'))}[/bold]  "
                    f"{_provenance(field, proposed[field], env_value)}"
                )
            console.print()
            continue
        console.print("[yellow]Please answer `y` (yes), `n` (no), or `e` (edit).[/yellow]")
