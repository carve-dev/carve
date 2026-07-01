"""``carve metrics costs/runs/agents`` — render the DB-backed rollups.

Each command follows ``carve schedule``'s setup block — ``load_config`` → resolve
the active target → build the engine → ``initialize_database`` → session factory →
construct :class:`~carve.core.observability.rollups.MetricsRollups`. ``--since``
(default ``7d``) is parsed up front (a bad window exits 2 before any DB read), and
``engine.dispose()`` runs in ``finally``. Rendering is Rich ``Table`` with
``escape()`` on every user-derived string.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from carve.core.config import ConfigError, load_config
from carve.core.observability.rollups import MetricsRollups, parse_since
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.targets.resolution import resolve_active_target

console = Console()


def _build_rollups() -> tuple[MetricsRollups, object]:
    """Run the shared setup block; return the rollup service + the engine (to dispose).

    Mirrors ``carve schedule``: ``load_config`` → resolve the active target →
    engine → migrate → session factory → :class:`MetricsRollups`. Exits 2 with a
    rendered ``ConfigError`` on a bad/absent config.
    """
    project_dir = Path.cwd()
    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc

    # Read the resolved ``--target`` flag at call time (mutated by the main
    # callback after import — a module-level import would bind ``None``).
    from carve.cli.main import ACTIVE_TARGET_FLAG

    resolve_active_target(ACTIVE_TARGET_FLAG, config)

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    return MetricsRollups(session_factory), engine


def _resolve_since(since: str) -> datetime:
    """Parse ``--since`` up front; exit 2 on a malformed window.

    ``OverflowError`` is caught alongside ``ValueError`` so an absurdly large
    window (e.g. ``--since 9999999999999999999999d``) — which overflows
    ``timedelta`` rather than raising ``ValueError`` — still exits 2 cleanly
    instead of surfacing a traceback.
    """
    try:
        return parse_since(since)
    except (ValueError, OverflowError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc


def _fmt_ms(value: float | None) -> str:
    """Render a millisecond duration compactly, or ``-`` for None."""
    return f"{value:.0f} ms" if value is not None else "-"


def costs_command(
    since: str = typer.Option("7d", "--since", help="Window, e.g. 7d, 24h, 30m, 2w."),
) -> None:
    """Roll up token→USD cost over agent invocations in the window."""
    cutoff = _resolve_since(since)
    rollups, engine = _build_rollups()
    try:
        result = rollups.costs(cutoff)
    finally:
        engine.dispose()  # type: ignore[attr-defined]

    table = Table(title=f"Cost (since {escape(since)})")
    table.add_column("Invocations", justify="right")
    table.add_column("Input tokens", justify="right")
    table.add_column("Output tokens", justify="right")
    table.add_column("Cost (USD)", justify="right")
    table.add_row(
        str(result.invocations),
        str(result.tokens_input),
        str(result.tokens_output),
        f"${result.cost_usd:.4f}",
    )
    console.print(table)
    raise typer.Exit(code=0)


def runs_command(
    since: str = typer.Option("7d", "--since", help="Window, e.g. 7d, 24h, 30m, 2w."),
) -> None:
    """Roll up run success/failure + median/p95 duration in the window."""
    cutoff = _resolve_since(since)
    rollups, engine = _build_rollups()
    try:
        result = rollups.runs(cutoff)
    finally:
        engine.dispose()  # type: ignore[attr-defined]

    summary = Table(title=f"Runs (since {escape(since)})")
    summary.add_column("Total", justify="right")
    summary.add_column("Succeeded", justify="right")
    summary.add_column("Failed", justify="right")
    summary.add_column("Median", justify="right")
    summary.add_column("p95", justify="right")
    summary.add_row(
        str(result.total),
        str(result.succeeded),
        str(result.failed),
        _fmt_ms(result.median_duration_ms),
        _fmt_ms(result.p95_duration_ms),
    )
    console.print(summary)

    if result.by_target:
        breakdown = Table(title="By pipeline / target")
        breakdown.add_column("Pipeline")
        breakdown.add_column("Target")
        breakdown.add_column("Total", justify="right")
        breakdown.add_column("Succeeded", justify="right")
        breakdown.add_column("Failed", justify="right")
        for group in result.by_target:
            breakdown.add_row(
                escape(group.pipeline_name or "-"),
                escape(group.target or "-"),
                str(group.total),
                str(group.succeeded),
                str(group.failed),
            )
        console.print(breakdown)
    raise typer.Exit(code=0)


def agents_command(
    since: str = typer.Option("7d", "--since", help="Window, e.g. 7d, 24h, 30m, 2w."),
) -> None:
    """Roll up per-agent invocation counts, token/cost totals, success rate, skill mix."""
    cutoff = _resolve_since(since)
    rollups, engine = _build_rollups()
    try:
        usages = rollups.agents(cutoff)
    finally:
        engine.dispose()  # type: ignore[attr-defined]

    if not usages:
        console.print("[yellow]No agent invocations in the window.[/yellow]")
        raise typer.Exit(code=0)

    table = Table(title=f"Agents (since {escape(since)})")
    table.add_column("Agent")
    table.add_column("Invocations", justify="right")
    table.add_column("Input tokens", justify="right")
    table.add_column("Output tokens", justify="right")
    table.add_column("Cost (USD)", justify="right")
    table.add_column("Success rate", justify="right")
    table.add_column("Skill calls", justify="right")
    for usage in usages:
        table.add_row(
            escape(usage.agent_name),
            str(usage.invocations),
            str(usage.tokens_input),
            str(usage.tokens_output),
            f"${usage.cost_usd:.4f}",
            f"{usage.success_rate * 100:.0f}%",
            str(usage.skill_calls),
        )
    console.print(table)
    raise typer.Exit(code=0)


__all__ = [
    "agents_command",
    "costs_command",
    "runs_command",
]
