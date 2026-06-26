"""``carve connect [component]`` — provision a component's backend on demand.

The explicit first-moment command for connect's lean slice: provision (resolve →
install → validate → pin) the bundled dbt engine for a dbt component, idempotent
and fail-closed. The orchestrator triggers the same loop implicitly on first dbt
use via the importable :func:`carve.core.connect.provision_dbt_engine` seam (the
full mid-task wiring is deferred — this command is the testable core).

Scope (lean slice): the bundled dbt-core engine for a dbt component. A managed
backend (``snowflake-native`` / ``dbt-cloud`` / ``remote``) or an external dbt
(``dbt_env == "external"``) wires/uses but installs nothing. Warehouse/source
connect and credential capture are a later connect slice.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from carve.core.config import Config, ConfigError, load_config
from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import ComponentConfig, ComponentMode, ComponentType
from carve.core.connect import (
    EngineInstallNotSupported,
    ProvisionOutcome,
    ProvisionResult,
    ValidationFailed,
    provision_dbt_engine,
)
from carve.core.connect.result import ConnectError
from carve.core.targets.resolution import (
    TargetResolutionError,
    resolve_active_target,
)
from carve.integrations.component_locator import (
    ComponentResolutionError,
    discover_components,
)

console = Console()

# Default dialect when the active target names no connection block — DuckDB is
# Carve's local-dev / test substrate, and the dbt-core engine it resolves to is
# the slice's runnable path.
_DEFAULT_DIALECT = "duckdb"


def command(
    component: str | None = typer.Argument(
        None,
        help="Component to provision. Defaults to the project's detected dbt component.",
    ),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help="Active target — selects the warehouse dialect that picks the engine.",
    ),
) -> None:
    """Provision ``component`` (or the detected dbt component) on demand."""
    root = project_dir.resolve()
    try:
        config = load_config(root)
    except ConfigError as exc:
        console.print(f"[red]Error:[/red] {exc.message}")
        raise typer.Exit(code=2) from exc

    paths = ProjectPaths.from_root(root)

    try:
        name, comp = _resolve_dbt_component(component, config, paths)
    except ComponentResolutionError as exc:
        console.print(f"[red]Error:[/red] {exc.message}")
        if exc.hint:
            console.print(f"  Hint: {exc.hint}")
        raise typer.Exit(code=2) from exc

    try:
        dialect = _resolve_dialect(target, config)
    except TargetResolutionError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    config_path = root / "carve.toml"
    install_root = _install_root(paths)

    try:
        result = provision_dbt_engine(
            comp,
            component_name=name,
            dialect=dialect,
            config_path=config_path,
            install_root=install_root,
        )
    except EngineInstallNotSupported as exc:
        # Fusion's binary fetch is deferred — resolution pinned it correctly, but
        # there's nothing to install yet. A clear, non-zero exit; no partial write.
        console.print(f"[yellow]![/yellow] {exc}")
        raise typer.Exit(code=1) from exc
    except ValidationFailed as exc:
        # Fail-closed: validate failed, so no pin was written. Say so explicitly.
        console.print("[red]Error:[/red] engine validation failed — no config written.")
        console.print(f"  {exc}")
        raise typer.Exit(code=1) from exc
    except ConnectError as exc:  # pragma: no cover - defensive catch-all
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _report(result, dialect)
    raise typer.Exit(code=0)


def _resolve_dbt_component(
    component: str | None,
    config: Config,
    paths: ProjectPaths,
) -> tuple[str, ComponentConfig]:
    """Resolve the dbt component to provision → its name + a `ComponentConfig`.

    A ``[components.<name>]`` block is used verbatim. A convention-discovered dbt
    component (no block) is given a minimal same-repo `ComponentConfig` for
    resolution. When ``component`` is omitted, the project's single detected dbt
    component is used; an ambiguous or absent dbt component is a resolution error.
    """
    if component is not None:
        block = config.components.get(component)
        if block is not None:
            if block.type is not ComponentType.DBT:
                raise ComponentResolutionError(
                    f"Component {component!r} is a {block.type.value} component, not dbt; "
                    "connect's lazy-engine provisioning is for dbt components.",
                    hint="This slice provisions the bundled dbt engine only.",
                )
            return component, block
        # Named but no block: accept it iff it's the convention dbt component.
        if _is_convention_dbt(component, paths):
            return component, _convention_dbt_config()
        raise ComponentResolutionError(
            f"No dbt component named {component!r}: no [components.{component}] block, "
            "and it is not the detected dbt project.",
            hint="Add a [components.<name>] block (type='dbt'), or run `carve init --with-dbt`.",
        )

    # No component named: provision the single detected dbt component.
    dbt_blocks = [
        (cname, c) for cname, c in config.components.items() if c.type is ComponentType.DBT
    ]
    dbt_discovered = [c for c in discover_components(paths) if c.type is ComponentType.DBT]
    if len(dbt_blocks) == 1 and not dbt_discovered:
        return dbt_blocks[0]
    if not dbt_blocks and len(dbt_discovered) == 1:
        return dbt_discovered[0].name, _convention_dbt_config()
    if not dbt_blocks and not dbt_discovered:
        raise ComponentResolutionError(
            "No dbt component found to connect.",
            hint="Run `carve init --with-dbt`, or name the component explicitly.",
        )
    raise ComponentResolutionError(
        "Multiple dbt components found; name the one to connect explicitly.",
        hint="e.g. `carve connect <name>`.",
    )


def _is_convention_dbt(name: str, paths: ProjectPaths) -> bool:
    """True iff ``name`` is the project's convention-discovered dbt component."""
    return any(c.type is ComponentType.DBT and c.name == name for c in discover_components(paths))


def _convention_dbt_config() -> ComponentConfig:
    """A minimal same-repo dbt `ComponentConfig` for a convention component.

    Convention dbt components carry no block, so synthesize the defaults the
    provision loop reads (no pin, no managed backend, bundled env). The pin
    write-back still requires a ``[components.<name>]`` block to exist; if one
    doesn't, ``pin_engine`` surfaces a clear "graduate the component first" error.
    """
    return ComponentConfig(type=ComponentType.DBT, mode=ComponentMode.SAME_REPO)


def _resolve_dialect(target: str | None, config: Config) -> str:
    """Resolve the warehouse dialect from the active target's connection block.

    The dialect is whichever connection family the resolved target appears
    under (``[connections.snowflake.<t>]`` → ``snowflake``;
    ``[connections.duckdb.<t>]`` → ``duckdb``) — the `sql` dialect axis. A target
    with no block falls back to DuckDB (the local-dev substrate), so a freshly
    scaffolded project provisions a runnable engine without warehouse creds.
    """
    active = resolve_active_target(target, config)
    if active in config.connections.snowflake:
        return "snowflake"
    if active in config.connections.duckdb:
        return "duckdb"
    return _DEFAULT_DIALECT


def _install_root(paths: ProjectPaths) -> Path:
    """The Carve-managed dir bundled engines are installed under.

    Co-located in the gitignored scratch dir (``.carve/engines/``). Injectable at
    the loop boundary so runtime worker-placement (deferred) can override it.
    """
    return paths.scratch_dir / "engines"


def _report(result: ProvisionResult, dialect: str) -> None:
    """Print a friendly summary of what the provision loop did."""
    name = result.component_name
    if result.outcome is ProvisionOutcome.PROVISIONED:
        assert result.pin is not None
        console.print(
            f"[green]✓[/green] Provisioned [bold]{result.pin.dbt_engine}[/bold] "
            f"{result.pin.dbt_version} for [bold]{name}[/bold] ({dialect})."
        )
        console.print("  Validated and pinned into carve.toml.")
        console.print(f"  Engine: {result.engine_path}")
    elif result.outcome is ProvisionOutcome.NOOP:
        assert result.pin is not None
        console.print(
            f"[green]✓[/green] [bold]{name}[/bold] already provisioned "
            f"({result.pin.dbt_engine} {result.pin.dbt_version}) — nothing to do."
        )
    elif result.outcome is ProvisionOutcome.MANAGED:
        console.print(
            f"[green]✓[/green] [bold]{name}[/bold] uses a managed backend — nothing to install."
        )
    else:  # EXTERNAL
        console.print(
            f"[green]✓[/green] [bold]{name}[/bold] uses an external dbt — nothing to install."
        )
