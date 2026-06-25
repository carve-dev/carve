"""CLI orchestration glue.

The modules here wire together pieces shipped by M1-02 through M1-06 —
config, state store, agent loop, runner, Snowflake connector — for the
high-level ``carve plan`` / ``carve build`` / ``carve run`` /
``carve pipelines`` commands. Keeping the orchestration out of the typer
command modules keeps those modules thin (arg parsing + exit-code
mapping) and makes the integration unit-testable without exercising
the typer harness.
"""

from carve.cli.orchestrator.builder import (
    BuildArtifact,
    BuildError,
    ConfigDriftError,
    PlanExpiredError,
    build_plan,
)
from carve.cli.orchestrator.listing import (
    render_logs,
    render_pipeline_detail,
    render_pipelines_table,
    render_recovery_tree,
    render_runs_table,
)
from carve.cli.orchestrator.planner import (
    PlanArtifact,
    PlanGenerationError,
    generate_plan,
)
from carve.cli.orchestrator.runner import (
    run_pipeline_by_name,
    run_pipeline_by_plan,
)

__all__ = [
    "BuildArtifact",
    "BuildError",
    "ConfigDriftError",
    "PlanArtifact",
    "PlanExpiredError",
    "PlanGenerationError",
    "build_plan",
    "generate_plan",
    "render_logs",
    "render_pipeline_detail",
    "render_pipelines_table",
    "render_recovery_tree",
    "render_runs_table",
    "run_pipeline_by_name",
    "run_pipeline_by_plan",
]
