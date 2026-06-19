"""``carve agents`` — list / show / create declarative agents."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from carve.core.agents.discovery import BUILTIN_AGENTS_DIR, AgentDiscovery
from carve.core.agents.lint import lint_agent_grants
from carve.core.agents.loader import AgentLoadError, load_agent_file
from carve.core.config import ConfigError, load_config
from carve.core.config.paths import ProjectPaths

app = typer.Typer(
    name="agents",
    help="List, show, and scaffold declarative agents.",
    no_args_is_help=True,
)

console = Console()


def _agents_dir(root: Path) -> Path:
    """Resolve the project's user ``agents_dir`` (default if unconfigured)."""
    agents_dir = "carve/agents"
    try:
        agents_dir = load_config(root).paths.agents_dir
    except ConfigError:
        pass
    return (root / agents_dir).resolve()


@app.command(name="list")
def list_agents(
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """List all discovered agents, marking built-in vs user override."""
    root = project_dir.resolve()
    user_dir = _agents_dir(root)
    discovery = AgentDiscovery.for_project(agents_dir=user_dir)

    # A user file at the same name as a built-in is an override; detect it
    # by comparing the per-root name sets.
    builtin_names = {a.name for a in discovery.discover() if _is_builtin(a.source_path)}
    user_names = {a.name for a in discovery.discover() if not _is_builtin(a.source_path)}

    registry = discovery.build_registry()
    if not registry.names():
        console.print(
            "[yellow]No agents discovered.[/yellow] "
            "Add one with `carve agents create <name>`."
        )
        raise typer.Exit(code=0)

    table = Table(title="Agents", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Provider")
    table.add_column("Max mode")
    table.add_column("Model")
    for spec in registry.specs():
        if spec.name in user_names and spec.name in builtin_names:
            provider = "user (overrides built-in)"
        elif spec.name in user_names:
            provider = "user"
        else:
            provider = "built-in"
        table.add_row(
            spec.name,
            provider,
            spec.capability.value,
            spec.model or "(install default)",
        )
    console.print(table)
    raise typer.Exit(code=0)


@app.command(name="show")
def show_agent(
    name: str = typer.Argument(..., help="The agent name to show."),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Show one agent's resolved definition + system prompt."""
    root = project_dir.resolve()
    user_dir = _agents_dir(root)
    discovery = AgentDiscovery.for_project(agents_dir=user_dir)
    registry = discovery.build_registry()
    if name not in registry:
        # Discovery silently skips a file that failed to load (one bad file
        # must not break the rest). Surface that here so a malformed file
        # isn't reported as merely "unknown" — the user sees *why* it's gone.
        load_error = _load_error_for(user_dir, name)
        if load_error is not None:
            console.print(
                f"[red]Agent {name!r} exists but failed to load:[/red] "
                f"{load_error}"
            )
            raise typer.Exit(code=1)
        console.print(f"[red]No agent named {name!r}.[/red] Known: {registry.names()}")
        raise typer.Exit(code=1)
    spec = registry.resolve(name)
    console.print(f"[bold]{spec.name}[/bold]")
    console.print(f"  description : {spec.description}")
    console.print(f"  max_mode    : {spec.capability.value}")
    console.print(f"  model       : {spec.model or '(install default)'}")
    console.print(f"  classifications: {list(spec.classifications)}")
    paths = ProjectPaths.from_root(root)
    console.print(f"  tools       : {[t.name for t in spec.tool_factory(paths)]}")
    console.print("\n[dim]--- system prompt ---[/dim]")
    console.print(spec.system_prompt)
    raise typer.Exit(code=0)


_AGENT_TEMPLATE = """\
---
name: {name}
description: Describe what this agent does and when to use it.
tools: [read_file, grep, glob]
max_mode: read_only
classifications: []
---
You are {name}. Replace this body with the agent's system prompt.
"""


@app.command(name="create")
def create_agent(
    name: str = typer.Argument(..., help="The new agent's name."),
    template: str | None = typer.Option(
        None,
        "--template",
        help="Copy an existing agent (built-in or user) as the starting point.",
    ),
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (the directory containing carve.toml).",
    ),
) -> None:
    """Scaffold a new agent ``.md`` under the user agents dir."""
    root = project_dir.resolve()
    user_dir = _agents_dir(root)
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / f"{name}.md"
    if target.exists():
        console.print(f"[red]{target} already exists.[/red]")
        raise typer.Exit(code=1)

    if template is not None:
        content = _template_from(root, user_dir, template, name)
    else:
        content = _AGENT_TEMPLATE.format(name=name)

    target.write_text(content, encoding="utf-8")

    # Verify the scaffold re-loads (a working agent, per the acceptance bar)
    # and surface any advisory lint warnings.
    try:
        parsed = load_agent_file(target)
    except AgentLoadError as exc:
        target.unlink(missing_ok=True)
        console.print(f"[red]Scaffold failed to load: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    lint_agent_grants(parsed)
    console.print(f"[green]Created {target}.[/green]")
    raise typer.Exit(code=0)


def _template_from(
    root: Path, user_dir: Path, template: str, new_name: str
) -> str:
    """Build new-agent content from an existing agent's file."""
    discovery = AgentDiscovery.for_project(agents_dir=user_dir)
    for agent in discovery.discover():
        if agent.name == template:
            text = agent.source_path.read_text(encoding="utf-8")
            # Swap the name: in YAML frontmatter the first `name:` line.
            return text.replace(f"name: {template}", f"name: {new_name}", 1)
    console.print(f"[red]No template agent named {template!r}.[/red]")
    raise typer.Exit(code=1)


def _load_error_for(user_dir: Path, name: str) -> str | None:
    """Return the load-error message for a ``{name}.md`` that failed to load.

    Discovery skips a malformed file silently; this re-attempts a direct
    load of the candidate file(s) so ``carve agents show`` can explain a
    missing agent that *exists on disk but won't parse*. Checks the user
    dir first (user overrides built-in), then the built-in dir. Returns
    ``None`` when no candidate file exists (a genuinely unknown agent) or
    the file loads fine (not the cause of the miss).
    """
    for directory in (user_dir, BUILTIN_AGENTS_DIR):
        candidate = (directory / f"{name}.md").resolve()
        if not candidate.is_file():
            continue
        try:
            load_agent_file(candidate)
        except AgentLoadError as exc:
            return str(exc)
        # The file loads cleanly — its declared `name:` differs from the
        # filename stem; not a load failure, so keep looking / fall through.
    return None


def _is_builtin(source_path: Path) -> bool:
    try:
        source_path.resolve().relative_to(BUILTIN_AGENTS_DIR.resolve())
        return True
    except ValueError:
        return False
