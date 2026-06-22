"""``carve auth`` — inspect and set up model-provider credentials.

Carve authenticates to Anthropic with either an ``ANTHROPIC_API_KEY`` or a
Claude-subscription OAuth bearer; precedence is resolved in one place
(:mod:`carve.core.agents.client_factory`). This surface lets a user see the
active mode (``status``) and mint a subscription OAuth token (``login``, a
thin wrapper over Claude Code's ``claude setup-token``).

This is *model-provider* auth — distinct from the REST/MCP API token
(``carve auth token …`` belongs to the rest-api capability).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer
from rich.console import Console

from carve.core.agents.client_factory import auth_status
from carve.core.config import ConfigError, load_config

app = typer.Typer(
    name="auth",
    help="Inspect and set up model-provider credentials (API key / Claude OAuth).",
    no_args_is_help=True,
)

console = Console()


@app.command(name="status")
def status(
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        help="Project root (directory containing carve.toml).",
    ),
) -> None:
    """Show the resolved model-auth mode (never any secret value)."""
    try:
        config = load_config(project_dir.resolve())
    except ConfigError as exc:
        console.print(f"[red]Error:[/red] {exc.message}")
        raise typer.Exit(code=2) from exc

    info = auth_status(config)
    if info.credential_present:
        console.print(f"[green]✓[/green] Authenticated via [bold]{info.mode}[/bold]")
        console.print(f"  Source: {info.source}")
    else:
        console.print(
            f"[yellow]![/yellow] No usable credential ([bold]{info.mode}[/bold])"
        )
        if info.note:
            console.print(f"  {info.note}")
    if info.hosted:
        console.print("  Hosted mode: subscription OAuth is disabled")
    console.print(f"  Default model: {info.default_model}")


@app.command(name="login")
def login() -> None:
    """Mint a Claude-subscription OAuth token via ``claude setup-token``.

    Carve runs no browser flow of its own — it delegates to Claude Code's
    ``claude setup-token``. After it prints a token, put it in ``.env`` as
    ``ANTHROPIC_AUTH_TOKEN`` (or ``CLAUDE_CODE_OAUTH_TOKEN``); Carve picks it
    up on the next run.
    """
    claude = shutil.which("claude")
    if claude is None:
        console.print("[yellow]![/yellow] `claude` (Claude Code) was not found on PATH.")
        console.print(
            "To use a Claude subscription, install Claude Code and run "
            "`claude setup-token`, then set the printed token as "
            "ANTHROPIC_AUTH_TOKEN in your .env."
        )
        console.print(
            "Or set ANTHROPIC_API_KEY to use a developer-portal API key instead."
        )
        raise typer.Exit(code=1)

    console.print("Running `claude setup-token` — complete the browser login…")
    result = subprocess.run([claude, "setup-token"], check=False)
    if result.returncode != 0:
        console.print(
            f"[red]`claude setup-token` exited with code {result.returncode}.[/red]"
        )
        raise typer.Exit(code=result.returncode)
    console.print(
        "[green]✓[/green] Token minted. Add it to your .env as "
        "[bold]ANTHROPIC_AUTH_TOKEN[/bold] (or CLAUDE_CODE_OAUTH_TOKEN). Leave "
        'auth_mode unset in carve/models.toml to auto-resolve, or set it to "oauth".'
    )
