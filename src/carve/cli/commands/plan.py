"""`carve plan` — design a pipeline (no files written).

Three modes:

* ``carve plan "<goal>"`` — design a brand-new pipeline.
* ``carve plan --refine <plan_id> "<feedback>"`` — refine a draft plan.
* ``carve plan --pipeline <name> "<change>"`` — propose a delta against
  an existing pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape as _escape

from carve.cli.orchestrator import generate_plan
from carve.cli.orchestrator.observers import RichConsoleObserver
from carve.cli.orchestrator.planner import PlanArtifact, PlanGenerationError
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
    goal: str = typer.Argument(..., help="The goal or feedback for the agent."),
    refine: str | None = typer.Option(
        None,
        "--refine",
        help=(
            "Refine the named draft plan. The new plan is recorded with "
            "`parent_plan_id = <plan_id>`."
        ),
    ),
    pipeline: str | None = typer.Option(
        None,
        "--pipeline",
        help=(
            "Modify an existing pipeline. The agent receives the current "
            "files in its context and proposes a delta."
        ),
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress live progress output; print only the final summary.",
    ),
) -> None:
    """Generate (or refine) a plan."""
    if refine is not None and pipeline is not None:
        console.print(
            "[red]✗[/red] --refine and --pipeline are mutually exclusive."
        )
        raise typer.Exit(code=2)

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

    parent_artifact = repository.get_plan(refine) if refine is not None else None

    try:
        try:
            artifact = generate_plan(
                goal=goal,
                config=config,
                project_dir=project_dir,
                repository=repository,
                observer=observer,
                parent_plan_id=refine,
                pipeline_name=pipeline,
            )
            # `--target` is read inside `generate_plan` via the module-level
            # `ACTIVE_TARGET_FLAG` slot wired by `_main_callback`; nothing
            # to forward here.
        finally:
            observer.close()
    except PlanGenerationError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except AgentError as exc:
        console.print(f"[red]✗ Agent error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    _render_plan_summary(artifact, refining=parent_artifact is not None)
    if parent_artifact is not None:
        _render_refine_diff(parent_artifact_design=_load_design(parent_artifact), new=artifact)
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_plan_summary(artifact: PlanArtifact, *, refining: bool) -> None:
    """Print the human-friendly plan summary derived from the design."""
    plan_id = _escape(artifact.id)
    headline = "Plan refined" if refining else "Plan generated"
    console.print(f"[green]✓[/green] {headline}: [bold]{plan_id}[/bold]")
    # Agent-emitted strings (pipeline name, description, destination,
    # tradeoffs, open questions) are escaped before interpolation —
    # otherwise a model that returns `[bold]` or `[/]` in its design
    # would corrupt the rendered output. Mirrors the M1.1-04 pattern.
    console.print(f"  Pipeline name:  {_escape(artifact.pipeline_name)}")
    if artifact.description:
        console.print(f"  Description:    {_escape(artifact.description)}")

    destination = artifact.design.get("destination") if isinstance(artifact.design, dict) else None
    if isinstance(destination, dict):
        db = _escape(str(destination.get("database", "?")))
        schema = _escape(str(destination.get("schema", "?")))
        table = _escape(str(destination.get("table", "?")))
        console.print(f"  Destination:    {db}.{schema}.{table}")

    transformation = (
        artifact.design.get("transformation") if isinstance(artifact.design, dict) else None
    )
    if isinstance(transformation, dict) and transformation.get("strategy"):
        console.print(f"  Strategy:       {_escape(str(transformation['strategy']))}")

    if artifact.requirements:
        rendered_reqs = ", ".join(_escape(r) for r in artifact.requirements)
        console.print(f"  Requirements:   {rendered_reqs}")

    estimates = artifact.design.get("estimates") if isinstance(artifact.design, dict) else None
    if isinstance(estimates, dict) and estimates:
        rendered = ", ".join(f"{_escape(str(k))}={_escape(str(v))}" for k, v in estimates.items())
        console.print(f"  Estimates:      {rendered}")

    tradeoffs = artifact.design.get("tradeoffs") if isinstance(artifact.design, dict) else None
    if isinstance(tradeoffs, list) and tradeoffs:
        console.print("  Tradeoffs:")
        for item in tradeoffs:
            console.print(f"    - {_escape(str(item))}")

    open_questions = (
        artifact.design.get("open_questions") if isinstance(artifact.design, dict) else None
    )
    if isinstance(open_questions, list) and open_questions:
        console.print("  [yellow]Open questions:[/yellow]")
        for item in open_questions:
            console.print(f"    - {_escape(str(item))}")

    console.print(
        f"  Tokens:         {artifact.tokens_input:,} in / {artifact.tokens_output:,} out"
    )
    console.print(f"  Cost:           ${artifact.cost_usd:.4f}")
    console.print()
    console.print(f"Next:  carve build {plan_id}")
    console.print(f"       carve plan --refine {plan_id} \"<feedback>\"")


def _load_design(plan_row: object) -> dict[str, Any]:
    """Pull a design dict out of a Plan ORM row's task_graph_json."""
    task_graph_raw = getattr(plan_row, "task_graph_json", None)
    if not isinstance(task_graph_raw, str):
        return {}
    try:
        task_graph = json.loads(task_graph_raw)
    except (TypeError, ValueError):
        return {}
    design = task_graph.get("design")
    return design if isinstance(design, dict) else {}


_DIFF_FIELDS: tuple[str, ...] = (
    "pipeline_name",
    "description",
    "source",
    "destination",
    "transformation",
    "columns",
    "requirements",
    "tradeoffs",
    "open_questions",
)


def _render_refine_diff(
    *,
    parent_artifact_design: dict[str, Any],
    new: PlanArtifact,
) -> None:
    """Print a field-by-field diff between the parent design and the new one."""
    console.print()
    console.print("[bold]Refinement diff:[/bold]")
    new_design = new.design if isinstance(new.design, dict) else {}
    for field in _DIFF_FIELDS:
        old_value = parent_artifact_design.get(field)
        new_value = new_design.get(field)
        if old_value == new_value:
            continue
        old_repr = json.dumps(old_value, sort_keys=True) if old_value is not None else "(unset)"
        new_repr = json.dumps(new_value, sort_keys=True) if new_value is not None else "(unset)"
        console.print(f"  [yellow]{field}[/yellow]")
        console.print(f"    - {old_repr}")
        console.print(f"    + {new_repr}")
