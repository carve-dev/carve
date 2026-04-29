"""Top-level typer app wiring up the eight carve subcommands."""

import typer

from carve.cli.commands import (
    apply,
    init,
    logs,
    plan,
    run,
    runs,
    serve,
    version,
)

app = typer.Typer(
    name="carve",
    help="AI-first data engineering framework. Carve structure from chaos.",
    no_args_is_help=True,
)

app.command(name="init")(init.command)
app.command(name="plan")(plan.command)
app.command(name="apply")(apply.command)
app.command(name="run")(run.command)
app.command(name="runs")(runs.command)
app.command(name="logs")(logs.command)
app.command(name="serve")(serve.command)
app.command(name="version")(version.command)


if __name__ == "__main__":
    app()
