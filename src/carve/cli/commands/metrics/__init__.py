"""``carve metrics`` — cost / runs / per-agent usage rollups (reference surface).

The rollups read the state store as **data**: ``costs`` sums token→USD over
``agent_invocations``, ``runs`` computes success/failure + duration over the
``runs`` table, and ``agents`` aggregates per-agent usage (+ skill-call mix). The
aggregation itself lives in :class:`~carve.core.observability.rollups.MetricsRollups`;
this CLI is the reference caller (the Increment-5 ``GET /metrics/*`` routers wire
onto the same service).
"""

from __future__ import annotations

import typer

from carve.cli.commands.metrics.commands import (
    agents_command,
    costs_command,
    runs_command,
)

app = typer.Typer(
    name="metrics",
    help="Roll up cost (token→USD), run success/failure, and per-agent usage.",
    no_args_is_help=True,
)

app.command(name="costs")(costs_command)
app.command(name="runs")(runs_command)
app.command(name="agents")(agents_command)


__all__ = ["app"]
