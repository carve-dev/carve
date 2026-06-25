"""`carve plan-and-build` — the one-shot convenience verb.

Plans a goal, then immediately builds the resulting plan, sharing one
repository + config across both steps (per the plan-build spec's
§"The Build entity": "the one-shot convenience (plan, then immediately
build — for trusted/CI flows)"). A thin wrapper over `generate_plan` +
`build_plan`; the builder's idempotency makes a re-run safe.

Because the plan is built the instant it's generated, the config-hash it
was planned against is the same config the build checks — so the drift
gate never trips on this path (drift only matters across a "plan now,
build later" gap). Exit codes match `carve build`: 1 = plan/agent error,
2 = build/plan-state error, 3 = config drift (defensive).
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape as _escape

from carve.cli.orchestrator import build_plan, generate_plan
from carve.cli.orchestrator.builder import (
    BuildError,
    ConfigDriftError,
    PlanExpiredError,
)
from carve.cli.orchestrator.observers import RichConsoleObserver
from carve.cli.orchestrator.planner import PlanGenerationError
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
    goal: str = typer.Argument(..., help="The goal for the agent to plan and build."),
    table: str | None = typer.Option(
        None,
        "--table",
        help="Pre-seed the destination table name (passed through to plan).",
    ),
    database: str | None = typer.Option(
        None,
        "--database",
        help="Pre-seed the destination database (passed through to plan).",
    ),
    schema: str | None = typer.Option(
        None,
        "--schema",
        help="Pre-seed the destination schema (passed through to plan).",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress live progress output; print only the final summaries.",
    ),
) -> None:
    """Generate a plan for ``goal`` and build it in one step."""
    destination_hint = _destination_hint_from_flags(table=table, database=database, schema=schema)

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

    observer = RichConsoleObserver(console, quiet=quiet)

    try:
        try:
            plan_artifact = generate_plan(
                goal=goal,
                config=config,
                project_dir=project_dir,
                repository=repository,
                observer=observer,
                destination_hint=destination_hint,
            )
        except PlanGenerationError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=1) from exc
        except AgentError as exc:
            console.print(f"[red]✗ Agent error:[/red] {exc}")
            raise typer.Exit(code=1) from exc

        console.print(
            f"[green]✓[/green] Planned [bold]{_escape(plan_artifact.id)}[/bold]; building…"
        )

        try:
            build_artifact = build_plan(
                plan_id=plan_artifact.id,
                config=config,
                project_dir=project_dir,
                repository=repository,
                observer=observer,
            )
        except ConfigDriftError as exc:
            console.print(f"[red]✗ Config drift:[/red] {exc}")
            raise typer.Exit(code=3) from exc
        except PlanExpiredError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=2) from exc
        except BuildError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=2) from exc
        except AgentError as exc:
            console.print(f"[red]✗ Agent error:[/red] {exc}")
            raise typer.Exit(code=1) from exc
    finally:
        observer.close()
        engine.dispose()

    if not build_artifact.success:
        console.print(
            "[red]✗[/red] Build did not write a `main.py`. Plan stays in "
            "phase=drafted; refine and retry."
        )
        if build_artifact.summary.strip():
            console.print(build_artifact.summary.strip())
        raise typer.Exit(code=1)

    console.print(
        f"[green]✓[/green] Built pipeline [bold]{_escape(build_artifact.pipeline_name)}[/bold]"
    )
    console.print(f"  Plan:           {_escape(build_artifact.plan_id)}")
    console.print(f"  Target:         {_escape(build_artifact.target)}")
    console.print(
        "  Build id:       "
        f"{_escape(build_artifact.build_id) if build_artifact.build_id else '(none)'}"
    )
    console.print("  Files written:")
    for path in build_artifact.files_written:
        console.print(f"    - {path}")
    console.print()
    console.print(f"Next:  carve run {build_artifact.pipeline_name}")
    raise typer.Exit(code=0)


def _destination_hint_from_flags(
    *,
    table: str | None,
    database: str | None,
    schema: str | None,
) -> dict[str, str] | None:
    """Bundle CLI-flag destination fields for the planner (mirrors `plan`)."""
    out: dict[str, str] = {}
    if table is not None and table.strip():
        out["table"] = table.strip()
    if database is not None and database.strip():
        out["database"] = database.strip()
    if schema is not None and schema.strip():
        out["schema"] = schema.strip()
    return out or None
