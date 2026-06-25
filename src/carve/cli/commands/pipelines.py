"""``carve pipelines`` — validate / list / show pipeline definitions.

This is the config-first surface over ``pipelines/*.toml``. ``validate`` is
the real schema + DAG gate (the function the verify loop and the engineer
call): it runs the **shipped**
:func:`carve.core.config.pipeline_schema.load_pipeline`, which catches missing
fields, duplicate ids, dangling ``depends_on``, cycles, bad cron, unresolvable
component names, and step-type/component-type mismatches — rendering the
structured :class:`PipelineError` and exiting non-zero on any failure.

``list`` and ``show`` are config views. The **run-history columns are deferred**
(the ``runs``/``step_runs`` tables are the Increment-4 runtime's); they render
a placeholder and a note rather than querying a table that doesn't exist.
``diff`` is a **deferred stub** — ``carve build``/``carve plan`` populate no
per-pipeline ``Build.manifest_json`` yet, so there is no stored manifest to
diff against.

This replaces the M1 single-command read-only lister (over the state
``Repository``); ``main.py`` registers the sub-group via ``add_typer``.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from carve.core.config import ConfigError, load_config
from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import (
    DbtStepConfig,
    DltStepConfig,
    Pipeline,
    PipelineError,
    SqlStepConfig,
    load_pipeline,
)
from carve.core.config.schema import ComponentConfig

app = typer.Typer(
    name="pipelines",
    help="Validate, list, and show pipeline definitions (pipelines/*.toml).",
    no_args_is_help=True,
)

console = Console()


# ---------------------------------------------------------------------------
# shared resolution
# ---------------------------------------------------------------------------


def _resolve(project_dir: Path) -> tuple[ProjectPaths, dict[str, ComponentConfig]]:
    """Resolve ``ProjectPaths`` + the ``[components.*]`` blocks for a project.

    Exits with code 2 (a clear, rendered ``ConfigError``) if ``carve.toml``
    can't be loaded — the same convention the other CLI commands use.
    """
    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    paths = ProjectPaths.from_root(project_dir)
    return paths, config.components


def _confined_pipeline_path(name: str, paths: ProjectPaths) -> Path:
    """Resolve ``pipelines/<name>.toml``, confined to directly under pipelines/.

    A ``name`` carrying separators / ``..`` must not escape the project (this
    surface is agent-reachable via the bash read-allowlist). Mirrors the shipped
    ``pipeline_inspect`` guard: resolve both, require the file sit *directly*
    under the resolved ``pipelines_dir``. On escape → exit 2 with a clear error.
    """
    pipelines_dir = paths.pipelines_dir.resolve()
    toml_path = (pipelines_dir / f"{name}.toml").resolve()
    if toml_path.parent != pipelines_dir:
        console.print(
            f"[red]Invalid pipeline name {name!r}:[/red] it resolves outside the "
            "project's pipelines/ directory."
        )
        raise typer.Exit(code=2)
    return toml_path


def _pipeline_files(paths: ProjectPaths) -> list[Path]:
    """Every ``pipelines/*.toml`` file, sorted by name (dotfiles skipped)."""
    if not paths.pipelines_dir.is_dir():
        return []
    return sorted(
        child
        for child in paths.pipelines_dir.iterdir()
        if child.is_file() and child.suffix == ".toml" and not child.name.startswith(".")
    )


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@app.command(name="validate")
def validate(
    name: str | None = typer.Argument(
        None,
        help="Pipeline name to validate. Omit to validate every pipeline.",
    ),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Schema + DAG validate a pipeline (or every pipeline).

    Runs ``load_pipeline`` for each target, catching schema errors, duplicate
    ids, dangling ``depends_on``, cycles, bad cron, unresolvable component
    names, and step-type/component-type mismatches. Exits non-zero on any
    failure with the structured error.
    """
    root = project_dir.resolve()
    paths, components = _resolve(root)

    if name is not None:
        toml_path = _confined_pipeline_path(name, paths)
        targets = [toml_path]
        if not toml_path.is_file():
            console.print(f"[red]No pipeline named {name!r} (looked for {toml_path}).[/red]")
            raise typer.Exit(code=2)
    else:
        targets = _pipeline_files(paths)
        if not targets:
            console.print("[yellow]No pipelines found under pipelines/.[/yellow]")
            raise typer.Exit(code=0)

    failures = 0
    for toml_path in targets:
        try:
            load_pipeline(toml_path, components=components, paths=paths)
        except PipelineError as exc:
            failures += 1
            console.print(f"[red]✗ {toml_path.stem}[/red]")
            console.print(escape(str(exc)))
        else:
            console.print(f"[green]✓ {toml_path.stem}[/green] — valid")

    if failures:
        plural = "s" if failures != 1 else ""
        console.print(f"\n[red]{failures} pipeline{plural} failed validation.[/red]")
        raise typer.Exit(code=1)
    console.print(f"\n[green]All {len(targets)} pipeline(s) valid.[/green]")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@app.command(name="list")
def list_pipelines(
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """List the pipelines/*.toml definitions with a config summary.

    The last-run summary column is deferred (the runs table is the runtime's,
    Increment 4); it renders ``—``.
    """
    root = project_dir.resolve()
    paths, components = _resolve(root)
    files = _pipeline_files(paths)

    if not files:
        console.print("[yellow]No pipelines found under pipelines/.[/yellow]")
        raise typer.Exit(code=0)

    table = Table(title="Pipelines")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Steps", justify="right")
    table.add_column("Schedule (seed)")
    table.add_column("Last run")  # deferred — runtime owns the runs table

    for toml_path in files:
        name = toml_path.stem
        try:
            pipeline = load_pipeline(toml_path, components=components, paths=paths)
        except PipelineError:
            table.add_row(name, "[red]invalid (run validate)[/red]", "—", "—", "—")
            continue
        cron = pipeline.seed_schedule.cron if pipeline.seed_schedule else "—"
        table.add_row(
            name,
            pipeline.pipeline.description or "—",
            str(len(pipeline.steps)),
            cron,
            "—",
        )

    console.print(table)
    console.print("[dim]Last-run history lands with the runtime (Increment 4).[/dim]")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@app.command(name="show")
def show(
    name: str = typer.Argument(..., help="Pipeline name to show."),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Show a pipeline's parsed config: metadata, seed schedule, ordered steps.

    The recent-run-history half is deferred (no runs table for pipelines yet —
    Increment 4); a note is printed in its place.
    """
    root = project_dir.resolve()
    paths, components = _resolve(root)

    toml_path = _confined_pipeline_path(name, paths)
    if not toml_path.is_file():
        console.print(f"[red]No pipeline named {name!r} (looked for {toml_path}).[/red]")
        raise typer.Exit(code=2)

    try:
        pipeline = load_pipeline(toml_path, components=components, paths=paths)
    except PipelineError as exc:
        console.print(f"[red]Pipeline {name!r} is invalid:[/red]")
        console.print(escape(str(exc)))
        raise typer.Exit(code=1) from exc

    _render_pipeline_detail(pipeline)


def _render_pipeline_detail(pipeline: Pipeline) -> None:
    console.print(f"[bold]Pipeline:[/bold] {pipeline.name}")
    if pipeline.pipeline.description:
        console.print(f"  description: {pipeline.pipeline.description}")
    if pipeline.pipeline.owner:
        console.print(f"  owner: {pipeline.pipeline.owner}")

    if pipeline.seed_schedule is not None:
        seed = pipeline.seed_schedule
        console.print(
            f"[bold]Seed schedule:[/bold] cron={seed.cron!r} "
            f"timezone={seed.timezone!r} target={seed.target!r} "
            "[dim](seed only — live schedule is data)[/dim]"
        )
    else:
        console.print("[bold]Seed schedule:[/bold] none (manual / API-triggered)")

    table = Table(title="Steps (in DAG order as written)")
    table.add_column("ID", style="bold")
    table.add_column("Type")
    table.add_column("Component / file")
    table.add_column("Depends on")
    table.add_column("Failure mode")

    for step in pipeline.steps:
        if isinstance(step, DltStepConfig):
            ref = f"component={step.component}"
        elif isinstance(step, DbtStepConfig):
            ref = f"component={step.component or '(detected)'} command={step.command}"
        elif isinstance(step, SqlStepConfig):
            ref = f"file={step.file} connection={step.connection}"
        else:  # pragma: no cover - the union is exhaustive
            ref = "—"
        table.add_row(
            step.id,
            step.type,
            ref,
            ", ".join(step.depends_on) or "—",
            step.failure_mode.mode,
        )

    console.print(table)
    console.print("[dim]Run history: available once the runtime ships (Increment 4).[/dim]")


# ---------------------------------------------------------------------------
# diff (deferred stub)
# ---------------------------------------------------------------------------


@app.command(name="diff")
def diff(
    name: str = typer.Argument(..., help="Pipeline name to diff."),
    against: str = typer.Option(
        ...,
        "--against",
        help="The build id to diff the current pipeline against.",
    ),
) -> None:
    """Diff a pipeline against an older build's manifest. DEFERRED.

    ``carve build``/``carve plan`` do not populate a per-pipeline
    ``Build.manifest_json`` for a ``pipelines/<name>.toml`` yet, so there is no
    stored manifest to diff against.
    """
    console.print(
        "[yellow]pipeline diff needs a build manifest; not yet — pipeline "
        "build-manifest population lands with plan-build wiring.[/yellow]"
    )
    raise typer.Exit(code=1)


__all__ = ["app"]
