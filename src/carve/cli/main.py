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
    pipelines,
    plan,
    run,
    runs,
    serve,
    version,
)
from carve.cli.dotenv import load_dotenv

app = typer.Typer(
    name="carve",
    help="AI-first data engineering framework. Carve structure from chaos.",
    no_args_is_help=True,
)


@app.callback()
def _main_callback(
    project_dir: Path = typer.Option(
        None,
        "--project-dir",
        help=(
            "Project root (the directory containing carve.toml). "
            "Defaults to the current directory."
        ),
    ),
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        help="Path to a .env file. Defaults to <project-dir>/.env.",
    ),
) -> None:
    """Auto-load a project-local ``.env`` before any subcommand runs.

    Existing shell vars win — ``.env`` provides defaults only. Set
    ``CARVE_NO_DOTENV=1`` to disable entirely (useful with direnv, mise, or
    similar env managers).
    """
    if os.environ.get("CARVE_NO_DOTENV") == "1":
        return
    root = project_dir if project_dir is not None else Path.cwd()
    target = env_file if env_file is not None else root / ".env"
    load_dotenv(target)


app.command(name="init")(init.command)
app.command(name="plan")(plan.command)
app.command(name="build")(build.command)
app.command(name="deploy")(deploy.command)
app.command(name="run")(run.command)
app.command(name="runs")(runs.command)
app.command(name="logs")(logs.command)
app.command(name="pipelines")(pipelines.command)
app.command(name="serve")(serve.command)
app.command(name="version")(version.command)


if __name__ == "__main__":
    app()
