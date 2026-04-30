"""CLI orchestration glue.

The modules here wire together pieces shipped by M1-02 through M1-06 —
config, state store, agent loop, runner, Snowflake connector — for the
high-level ``carve plan`` and ``carve apply`` commands. Keeping the
orchestration out of the typer command modules keeps those modules thin
(arg parsing + exit-code mapping only) and makes the integration unit-
testable without exercising the typer harness.
"""

from carve.cli.orchestrator.applier import apply_plan
from carve.cli.orchestrator.listing import render_logs, render_runs_table
from carve.cli.orchestrator.planner import PlanArtifact, generate_plan

__all__ = [
    "PlanArtifact",
    "apply_plan",
    "generate_plan",
    "render_logs",
    "render_runs_table",
]
