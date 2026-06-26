"""``carve memory`` — read and edit the project-memory files.

Scope: ``show`` (list / print a file / print a pipeline's bundle), ``edit``
(open a file in ``$EDITOR`` and write it directly), ``append-decision`` (append a
dated entry to ``decisions.md`` — a write that doesn't need the plan/build gate),
and ``refresh`` (run the dbt convention-inference engine and write the inferred
``conventions.md`` — the brownfield entry point). Deferred (tracked): the
reviewed plan/build promotion path for ``standards`` / sidecar edits.
"""

from __future__ import annotations

from datetime import date as date_cls
from pathlib import Path

import click
import typer
from rich.console import Console

from carve.core.config.paths import ProjectPaths
from carve.core.memory import (
    DecisionAlreadyExists,
    MemoryFile,
    MemoryLoader,
    MemoryWriter,
    select_for_task,
)
from carve.integrations.component_locator import (
    ComponentResolutionError,
    _detect_dbt_project,
)
from carve.integrations.dbt.conventions import (
    infer_conventions,
    render_conventions_md,
)

console = Console()

app = typer.Typer(
    name="memory",
    help="Read and edit project memory (conventions, standards, decisions, sidecars).",
    no_args_is_help=True,
)

# The user-editable named files and how the loader reads each.
_CORE_KINDS = ("conventions", "standards", "decisions")

_PROJECT_DIR_OPTION = typer.Option(
    Path("."),
    "--project-dir",
    help="Project root (the directory containing carve.toml).",
)


def _loader(project_dir: Path) -> tuple[ProjectPaths, MemoryLoader]:
    paths = ProjectPaths.from_root(project_dir)
    return paths, MemoryLoader(paths)


def _load_core(loader: MemoryLoader, kind: str) -> MemoryFile | None:
    return {
        "conventions": loader.load_conventions,
        "standards": loader.load_standards,
        "decisions": loader.load_decisions,
    }[kind]()


def _launch_editor(path: Path) -> None:
    # Wrapped so tests can monkeypatch the editor launch. `click.edit` honors
    # $VISUAL/$EDITOR and edits the file in place.
    click.edit(filename=str(path))


@app.command(name="show")
def show(
    kind: str | None = typer.Argument(
        None,
        help="Which file to print: conventions | standards | decisions. Omit to list all.",
    ),
    pipeline: str | None = typer.Option(None, "--pipeline", help="Show the bundle for a pipeline."),
    el: str | None = typer.Option(None, "--el", help="Show the sidecar for an el artifact."),
    project_dir: Path = _PROJECT_DIR_OPTION,
) -> None:
    """List memory files, print one file, or print a pipeline/el bundle."""
    paths, loader = _loader(project_dir.resolve())

    if pipeline is not None or el is not None:
        bundle = select_for_task(
            classification="",
            pipeline_targets=[pipeline] if pipeline else [],
            el_targets=[el] if el else [],
            is_investigative=False,
            loader=loader,
        )
        _print_section("conventions", bundle.conventions)
        _print_section("standards", bundle.standards)
        for name, mf in bundle.pipeline_sidecars.items():
            _print_section(f"pipeline:{name}", mf)
        for name, mf in bundle.el_sidecars.items():
            _print_section(f"el:{name}", mf)
        raise typer.Exit(code=0)

    if kind is not None:
        if kind not in _CORE_KINDS:
            console.print(
                f"[red]Error:[/red] unknown memory file '{kind}'. "
                f"Choose one of: {', '.join(_CORE_KINDS)}."
            )
            raise typer.Exit(code=2)
        memory_file = _load_core(loader, kind)
        if memory_file is None:
            console.print(f"[yellow]![/yellow] {kind}.md not present.")
            raise typer.Exit(code=0)
        console.print(memory_file.contents, markup=False, soft_wrap=True)
        raise typer.Exit(code=0)

    # No arg → list the memory files with size + mtime.
    _list_memory(paths, loader)
    raise typer.Exit(code=0)


def _print_section(label: str, memory_file: MemoryFile | None) -> None:
    if memory_file is None:
        return
    console.print(f"[bold cyan]# {label}[/bold cyan] ({memory_file.path})")
    console.print(memory_file.contents, markup=False, soft_wrap=True)
    console.print()


def _list_memory(paths: ProjectPaths, loader: MemoryLoader) -> None:
    console.print("[bold]Project memory[/bold]")
    rows: list[tuple[str, MemoryFile | None]] = [
        (kind, _load_core(loader, kind)) for kind in _CORE_KINDS
    ]
    if paths.pipelines_dir.is_dir():
        for md in sorted(paths.pipelines_dir.glob("*.md")):
            rows.append((f"pipeline:{md.stem}", loader.load_pipeline_sidecar(md.stem)))
    if paths.el_dir.is_dir():
        for notes in sorted(paths.el_dir.glob("*/NOTES.md")):
            rows.append((f"el:{notes.parent.name}", loader.load_el_sidecar(notes.parent.name)))
    for label, memory_file in rows:
        if memory_file is None:
            console.print(f"  [dim]{label:<24} (absent)[/dim]")
        else:
            stamp = memory_file.mtime.strftime("%Y-%m-%d %H:%M")
            console.print(f"  {label:<24} {memory_file.size_bytes:>7} B   {stamp}")


@app.command(name="edit")
def edit(
    kind: str | None = typer.Argument(
        None, help="Which file to edit: conventions | standards | decisions."
    ),
    pipeline: str | None = typer.Option(None, "--pipeline", help="Edit a pipeline sidecar."),
    el: str | None = typer.Option(None, "--el", help="Edit an el artifact's NOTES.md."),
    project_dir: Path = _PROJECT_DIR_OPTION,
) -> None:
    """Open a memory file in $EDITOR and write it directly.

    The reviewed plan/build promotion path for standards/sidecars is deferred;
    this lean command always writes the file directly (the spec's --direct
    escape hatch).
    """
    paths, loader = _loader(project_dir.resolve())

    target = _edit_target(paths, kind=kind, pipeline=pipeline, el=el)
    if target is None:
        console.print(
            "[red]Error:[/red] specify exactly one of: a file "
            f"({' | '.join(_CORE_KINDS)}), --pipeline <name>, or --el <name>."
        )
        raise typer.Exit(code=2)

    target.parent.mkdir(parents=True, exist_ok=True)
    existed_before = target.exists()
    if not existed_before:
        target.write_text("", encoding="utf-8")
    _launch_editor(target)
    loader.invalidate(target)
    # An abandoned edit of a brand-new file (editor quit without saving) must
    # not leave an empty file behind to pollute `show`/bundles.
    if not existed_before and target.exists() and target.stat().st_size == 0:
        target.unlink()
        console.print(f"[yellow]![/yellow] {target} left empty — not created.")
        raise typer.Exit(code=0)
    console.print(f"[green]✓[/green] wrote {target}")
    raise typer.Exit(code=0)


def _edit_target(
    paths: ProjectPaths, *, kind: str | None, pipeline: str | None, el: str | None
) -> Path | None:
    chosen = [v for v in (kind, pipeline, el) if v is not None]
    if len(chosen) != 1:
        return None
    if pipeline is not None:
        return paths.pipelines_dir / f"{pipeline}.md"
    if el is not None:
        return paths.el_dir / el / "NOTES.md"
    if kind not in _CORE_KINDS:
        return None
    return paths.carve_dir / f"{kind}.md"


@app.command(name="append-decision")
def append_decision(
    title: str = typer.Argument(..., help="Short decision title."),
    body: str | None = typer.Option(None, "--body", help="Decision body (prompted if omitted)."),
    reviewers: str = typer.Option("", "--reviewers", help="Comma-separated reviewers."),
    date: str | None = typer.Option(None, "--date", help="ISO date (default: today)."),
    force: bool = typer.Option(False, "--force", help="Append even if the entry already exists."),
    project_dir: Path = _PROJECT_DIR_OPTION,
) -> None:
    """Append a dated entry to decisions.md (no plan/build required)."""
    paths, loader = _loader(project_dir.resolve())

    try:
        entry_date = date_cls.fromisoformat(date) if date else date_cls.today()
    except ValueError as exc:
        console.print(f"[red]Error:[/red] invalid --date {date!r}; use YYYY-MM-DD.")
        raise typer.Exit(code=2) from exc

    # Interactive (no --body) → prompt for body and, if not supplied, reviewers.
    interactive = body is None
    text = body if body is not None else typer.prompt("Decision body")
    if interactive and not reviewers:
        reviewers = typer.prompt("Reviewers (comma-separated)", default="")
    reviewer_list = [r.strip() for r in reviewers.split(",") if r.strip()]

    writer = MemoryWriter(paths, loader)
    try:
        path = writer.append_decision(
            date=entry_date,
            title=title,
            body=text,
            reviewers=reviewer_list,
            force=force,
        )
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except DecisionAlreadyExists as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    console.print(f"[green]✓[/green] appended decision to {path}")
    raise typer.Exit(code=0)


@app.command(name="refresh")
def refresh(
    project_dir: Path = _PROJECT_DIR_OPTION,
) -> None:
    """Infer the dbt project's conventions and write ``conventions.md``.

    The brownfield entry point: resolves the same-repo dbt project (root or one
    level down), runs the convention-inference engine over its manifest + model
    tree + ``dbt_project.yml``, and overwrites ``carve/conventions.md`` with the
    inferred naming / layout / materialization / test conventions so the dbt
    engineer authors in that style and dbt-qa flags departures from it.

    No plan/build gate: conventions are Carve-derived facts about the project, and
    re-running inference is always safe and idempotent.
    """
    resolved_dir = project_dir.resolve()
    paths, loader = _loader(resolved_dir)

    try:
        dbt_root = _detect_dbt_project(paths, required=False)
    except ComponentResolutionError as exc:
        # Ambiguous (multiple projects) is the only raising case at required=False.
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    if dbt_root is None:
        console.print(
            "[yellow]![/yellow] No dbt project found (looked at the root and one "
            "level down). Nothing to infer.\n"
            "  Run `carve init --with-dbt` to scaffold one, or add a dbt component."
        )
        raise typer.Exit(code=0)

    conventions = infer_conventions(dbt_root)
    markdown = render_conventions_md(conventions)

    writer = MemoryWriter(paths, loader)
    try:
        path = writer.write_conventions(markdown)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    if conventions.has_any:
        layers = ", ".join(
            layer
            for layer in ("staging", "intermediate", "marts")
            if conventions.layer(layer).present
        )
        console.print(
            f"[green]✓[/green] inferred conventions from {dbt_root} "
            f"({conventions.model_count} model(s); layers: {layers or 'none'}) → {path}"
        )
    else:
        console.print(
            f"[green]✓[/green] wrote {path} (no conventions detected yet — the dbt "
            "project has no models)."
        )
    raise typer.Exit(code=0)
