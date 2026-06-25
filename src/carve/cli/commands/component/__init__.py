"""``carve component`` / ``carve components`` — graduation + inspection.

Two surfaces share this module:

* ``carve component <name> --separate-remote <url> [--ref|--branch]`` /
  ``--separate-local <path>`` / ``--same-repo`` — **graduation**: write (or, for
  ``--same-repo``, remove) the ``[components.<name>]`` block in ``carve.toml``,
  clone + validate a separate-remote workspace, and backfill omitted dbt-step
  names. A pure control-plane edit (no state migration, no re-runs).
* ``carve components show [<name>]`` — the always-on inspection surface that
  makes the simple-mode convention legible: every convention-discovered +
  block-defined component with its type / mode / resolved path; ``show <name>``
  adds the one component's resolution detail + which pipeline steps reference
  it.

The clone (``sync_workspace``) is referenced via a module-level seam so tests
inject a fake without spawning ``git``.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from carve.cli.commands.component.graduation import (
    GraduationError,
    backfill_dbt_step_components,
    infer_component_type,
    remove_component_block,
    write_component_block,
)
from carve.core.config import ConfigError, load_config
from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import (
    DbtStepConfig,
    DltStepConfig,
    PipelineError,
    load_pipeline,
)
from carve.core.config.schema import ComponentConfig, ComponentMode, ComponentType
from carve.integrations.component_locator import (
    ComponentResolutionError,
    ResolvedComponent,
    discover_components,
    resolve_component,
)

# The clone primitive, referenced via the module so tests can inject a fake
# `sync_workspace` (no real git) by monkeypatching this name.
from carve.integrations.workspace_cache import WorkspaceSyncError, sync_workspace

console = Console()

# `carve component` (singular) carries the graduation verb; `carve components`
# (plural) carries `show`. The singular is a LEAF command (a typer *group* whose
# callback takes a required positional argument can't bind it when mounted as a
# sub-app — click consumes the value looking for a subcommand — so `component` is
# registered on the top-level app via `app.command(name="component")(component)`
# in main.py, not as an `add_typer` group). The plural stays a group (it carries
# `show`).
components_app = typer.Typer(
    name="components",
    help="Inspect the project's components (convention + graduated).",
    no_args_is_help=True,
)


def _resolve(project_dir: Path) -> tuple[ProjectPaths, dict[str, ComponentConfig], Path]:
    """Resolve ``ProjectPaths`` + ``[components.*]`` blocks + the carve.toml path."""
    try:
        config = load_config(project_dir)  # validates the project and binds the config
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    paths = ProjectPaths.from_root(project_dir)
    return paths, config.components, project_dir / "carve.toml"


# ---------------------------------------------------------------------------
# graduation: carve component <name> ...
# ---------------------------------------------------------------------------


def component(
    name: str = typer.Argument(..., help="Component name to graduate."),
    separate_remote: str | None = typer.Option(
        None,
        "--separate-remote",
        help="Graduate to a remote git repo at this URL.",
    ),
    separate_local: str | None = typer.Option(
        None,
        "--separate-local",
        help="Graduate to a local path outside the control-plane tree.",
    ),
    same_repo: bool = typer.Option(
        False,
        "--same-repo",
        help="Reverse graduation: drop the [components.<name>] block.",
    ),
    ref: str | None = typer.Option(
        None, "--ref", help="Pin the remote at this commit SHA or tag (separate-remote)."
    ),
    branch: str | None = typer.Option(
        None, "--branch", help="Track this branch HEAD (separate-remote)."
    ),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Graduate ``<name>`` to its own repo, or reverse it with ``--same-repo``."""
    # A component name is a single path segment — reject separators / traversal /
    # NUL before any work so a `..`-laden name can't write a junk
    # `[components."../x"]` block or flow into a graduation path.
    if "/" in name or "\\" in name or ".." in name or "\x00" in name:
        console.print(
            f"[red]Invalid component name {name!r}:[/red] a component name must be a "
            "single path segment (no '/', '\\', '..', or NUL)."
        )
        raise typer.Exit(code=2)

    chosen = [
        flag
        for flag, on in (
            ("--separate-remote", separate_remote is not None),
            ("--separate-local", separate_local is not None),
            ("--same-repo", same_repo),
        )
        if on
    ]
    if len(chosen) != 1:
        console.print(
            "[red]Pass exactly one of --separate-remote, --separate-local, or --same-repo.[/red]"
        )
        raise typer.Exit(code=2)

    root = project_dir.resolve()
    paths, _components, config_path = _resolve(root)

    if same_repo:
        _reverse_graduation(name, config_path=config_path)
        return
    if separate_local is not None:
        _graduate_local(name, separate_local, paths=paths, config_path=config_path)
        return
    assert separate_remote is not None
    _graduate_remote(
        name,
        separate_remote,
        ref=ref,
        branch=branch,
        paths=paths,
        config_path=config_path,
    )


def _graduate_remote(
    name: str,
    url: str,
    *,
    ref: str | None,
    branch: str | None,
    paths: ProjectPaths,
    config_path: Path,
) -> None:
    try:
        component_type = infer_component_type(name, paths)
    except GraduationError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc

    # 1. Write the block (comment-preserving tomlkit).
    try:
        write_component_block(
            name,
            config_path=config_path,
            component_type=component_type,
            mode=ComponentMode.SEPARATE_REMOTE,
            url=url,
            ref=ref,
            branch=branch,
        )
    except GraduationError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc

    # 2. Clone + validate the workspace; roll back the block on failure so a
    #    bad URL doesn't leave a half-graduated carve.toml.
    try:
        sync_workspace(name, url, branch, paths, ref=ref)
        config = load_config(paths.root)
        resolve_component(name, components=config.components, paths=paths)
    except (WorkspaceSyncError, ComponentResolutionError, ConfigError) as exc:
        remove_component_block(name, config_path=config_path)
        console.print(f"[red]Graduation failed (rolled back carve.toml):[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc

    # 3. Backfill omitted dbt-step component names. ONLY for a dbt component —
    #    backfilling a dlt name into omitting dbt steps would point those steps
    #    at a dlt component and break step-type/component-type validation.
    backfilled = (
        backfill_dbt_step_components(name, pipelines_dir=paths.pipelines_dir)
        if component_type is ComponentType.DBT
        else []
    )
    _report_graduation(name, component_type.value, "separate-remote", url, backfilled)


def _graduate_local(
    name: str,
    path: str,
    *,
    paths: ProjectPaths,
    config_path: Path,
) -> None:
    try:
        component_type = infer_component_type(name, paths)
    except GraduationError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc

    try:
        write_component_block(
            name,
            config_path=config_path,
            component_type=component_type,
            mode=ComponentMode.SEPARATE_LOCAL,
            path=path,
        )
    except GraduationError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc

    # Validate it resolves (the path must exist), rolling back on failure.
    try:
        config = load_config(paths.root)
        resolve_component(name, components=config.components, paths=paths)
    except (ComponentResolutionError, ConfigError) as exc:
        remove_component_block(name, config_path=config_path)
        console.print(f"[red]Graduation failed (rolled back carve.toml):[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc

    # Backfill omitted dbt-step component names — ONLY for a dbt component (a dlt
    # name backfilled into omitting dbt steps would break validation).
    backfilled = (
        backfill_dbt_step_components(name, pipelines_dir=paths.pipelines_dir)
        if component_type is ComponentType.DBT
        else []
    )
    _report_graduation(name, component_type.value, "separate-local", path, backfilled)


def _reverse_graduation(name: str, *, config_path: Path) -> None:
    try:
        remove_component_block(name, config_path=config_path)
    except GraduationError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    console.print(
        f"[green]✓[/green] Reversed graduation: dropped "
        rf"\[components.{name}] (resolves by convention again)."
    )


def _report_graduation(
    name: str, type_: str, mode: str, location: str, backfilled: list[str]
) -> None:
    console.print(f"[green]✓[/green] Graduated [bold]{name}[/bold] ({type_}, {mode}) → {location}")
    if backfilled:
        console.print(
            f"  Backfilled component={name!r} into {len(backfilled)} dbt step(s): "
            + ", ".join(backfilled)
        )


# ---------------------------------------------------------------------------
# inspection: carve components show [<name>]
# ---------------------------------------------------------------------------


@components_app.command(name="show")
def show(
    name: str | None = typer.Argument(
        None, help="Component name to show in detail. Omit to list all."
    ),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """List every component (convention + graduated), or one in detail."""
    root = project_dir.resolve()
    paths, components, _config_path = _resolve(root)

    if name is None:
        _show_all(paths, components)
    else:
        _show_one(name, paths, components)


def _all_component_names(paths: ProjectPaths, components: dict[str, ComponentConfig]) -> list[str]:
    """The merged set of convention-discovered + block-defined component names."""
    names = {r.name for r in discover_components(paths)} | set(components)
    return sorted(names)


def _resolved_or_none(
    name: str, paths: ProjectPaths, components: dict[str, ComponentConfig]
) -> ResolvedComponent | None:
    try:
        return resolve_component(name, components=components, paths=paths)
    except ComponentResolutionError:
        return None


def _show_all(paths: ProjectPaths, components: dict[str, ComponentConfig]) -> None:
    names = _all_component_names(paths, components)
    if not names:
        console.print(
            "[yellow]No components found[/yellow] (no el/<name>/ dirs, no detected "
            r"dbt project, no \[components.*] blocks)."
        )
        raise typer.Exit(code=0)

    table = Table(title="Components")
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Mode")
    table.add_column("URL / path")
    table.add_column("Ref / branch")
    table.add_column("Resolved path")

    for name in names:
        block = components.get(name)
        mode = block.mode.value if block is not None else "convention"
        url_or_path = "—"
        ref_branch = "—"
        if block is not None:
            url_or_path = block.url or block.path or "—"
            ref_branch = block.ref or block.branch or "—"
        resolved = _resolved_or_none(name, paths, components)
        type_ = (
            resolved.type.value
            if resolved is not None
            else (block.type.value if block is not None else "?")
        )
        resolved_path = (
            str(resolved.code_path) if resolved is not None else "[red]unresolvable[/red]"
        )
        table.add_row(name, type_, mode, url_or_path, ref_branch, resolved_path)

    console.print(table)


def _show_one(name: str, paths: ProjectPaths, components: dict[str, ComponentConfig]) -> None:
    if name not in _all_component_names(paths, components):
        console.print(f"[red]No component named {name!r}.[/red]")
        raise typer.Exit(code=2)

    block = components.get(name)
    resolved = _resolved_or_none(name, paths, components)

    console.print(f"[bold]Component:[/bold] {name}")
    if block is not None:
        console.print(f"  mode: {block.mode.value}")
        console.print(f"  type: {block.type.value}")
        if block.url:
            console.print(f"  url: {block.url}")
        if block.path:
            console.print(f"  path: {block.path}")
        if block.ref:
            console.print(f"  ref: {block.ref}")
        if block.branch:
            console.print(f"  branch: {block.branch}")
    else:
        console.print(r"  mode: convention (simple mode — no \[components.*] block)")
        if resolved is not None:
            console.print(f"  type: {resolved.type.value}")

    if resolved is not None:
        console.print(f"  resolved path: {resolved.code_path}")
    else:
        console.print("  [red]resolved path: unresolvable[/red]")

    referencing = _steps_referencing(name, paths, components)
    if referencing:
        console.print("[bold]Referenced by pipeline steps:[/bold]")
        for ref in referencing:
            console.print(f"  • {ref}")
    else:
        console.print("[dim]Not referenced by any pipeline step.[/dim]")


def _steps_referencing(
    name: str, paths: ProjectPaths, components: dict[str, ComponentConfig]
) -> list[str]:
    """``"<pipeline>:<step_id>"`` for every dlt/dbt step referencing ``name``."""
    out: list[str] = []
    if not paths.pipelines_dir.is_dir():
        return out
    for toml_path in sorted(paths.pipelines_dir.glob("*.toml")):
        if toml_path.name.startswith("."):
            continue
        try:
            pipeline = load_pipeline(toml_path, components=components, paths=paths)
        except PipelineError:
            continue  # a broken pipeline is surfaced by `pipelines validate`, not here
        for step in pipeline.steps:
            if isinstance(step, (DltStepConfig, DbtStepConfig)) and step.component == name:
                out.append(f"{pipeline.name}:{step.id}")
    return out


__all__ = ["component", "components_app"]
