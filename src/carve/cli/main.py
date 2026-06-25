"""Top-level typer app wiring up the carve subcommands."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from carve.cli.commands import (
    build,
    deploy,
    init,
    logs,
    mcp_servers,
    pipelines,
    plan,
    plan_and_build,
    runs,
    serve,
    version,
)
from carve.cli.commands.agents import app as agents_app
from carve.cli.commands.auth import app as auth_app
from carve.cli.commands.component import component as component_cmd
from carve.cli.commands.component import components_app
from carve.cli.commands.el import app as el_app
from carve.cli.commands.memory import app as memory_app
from carve.cli.commands.schedule import app as schedule_app
from carve.cli.commands.skills import app as skills_app
from carve.cli.commands.target import app as target_app
from carve.cli.dotenv import load_dotenv

app = typer.Typer(
    name="carve",
    help="AI-first data engineering framework. Carve structure from chaos.",
    no_args_is_help=True,
)


# Module-level slot for the resolved ``--target`` flag. Subcommands read it
# via ``carve.cli.main.ACTIVE_TARGET_FLAG`` rather than the typer context to
# keep their signatures clean. ``None`` means "no flag passed"; downstream
# code falls through to ``CARVE_TARGET`` env var or ``default_target`` from
# config (see ``carve.core.targets.resolution.resolve_active_target``).
ACTIVE_TARGET_FLAG: str | None = None


@app.callback()
def _main_callback(
    project_dir: Path = typer.Option(
        None,
        "--project-dir",
        help=(
            "Project root (the directory containing carve.toml). Defaults to the current directory."
        ),
    ),
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        help="Path to a .env file. Defaults to <project-dir>/.env.",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help=(
            "Active target (e.g. dev, staging, prod). Overrides "
            "$CARVE_TARGET and `default_target` in carve.toml."
        ),
    ),
) -> None:
    """Auto-load a project-local ``.env`` before any subcommand runs.

    Existing shell vars win — ``.env`` provides defaults only. Set
    ``CARVE_NO_DOTENV=1`` to disable entirely (useful with direnv, mise, or
    similar env managers).
    """
    global ACTIVE_TARGET_FLAG
    ACTIVE_TARGET_FLAG = target

    if os.environ.get("CARVE_NO_DOTENV") == "1":
        return
    root = project_dir if project_dir is not None else Path.cwd()
    env_target = env_file if env_file is not None else root / ".env"
    load_dotenv(env_target)


app.command(name="init")(init.command)
app.command(name="plan")(plan.command)
app.command(name="build")(build.command)
app.command(name="plan-and-build")(plan_and_build.command)
app.command(name="deploy")(deploy.command)
app.command(name="runs")(runs.command)
app.command(name="logs")(logs.command)
app.command(name="serve")(serve.command)
app.command(name="version")(version.command)
app.add_typer(auth_app, name="auth")
app.add_typer(target_app, name="target")
app.add_typer(el_app, name="el")
app.add_typer(pipelines.app, name="pipelines")
app.command(name="component")(component_cmd)
app.add_typer(components_app, name="components")
app.add_typer(schedule_app, name="schedule")
app.add_typer(memory_app, name="memory")
app.add_typer(agents_app, name="agents")
app.add_typer(skills_app, name="skills")
app.add_typer(mcp_servers.app, name="mcp-servers")


if __name__ == "__main__":
    app()
