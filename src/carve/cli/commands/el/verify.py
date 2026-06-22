"""``carve el verify`` — read-only verification of a target's state.

Resolves the artifact's most recent successful Build, parses its plan
design for the expected destinations, and runs `core.deploy.verifier`'s
checks against the runtime role. No writes; safe to invoke any time.

Same checks the deploy command's Phase 6 runs internally — this is
the standalone surface useful for ad-hoc sanity checks, separate-from-
deploy CI gates, and post-manual-change auditing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from carve.core.config import ConfigError, load_config
from carve.core.config.schema import Config
from carve.core.connectors.exceptions import SnowflakeError
from carve.core.connectors.snowflake import SnowflakePool
from carve.core.deploy import run_verify
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Plan
from carve.core.targets.names import (
    InvalidArtifactNameError,
    InvalidTargetNameError,
    validate_artifact_name,
    validate_target_name,
)

logger = logging.getLogger(__name__)
console = Console()


def command(
    name: str = typer.Argument(..., help="EL artifact name to verify."),
    target: str = typer.Option(..., "--target", help="Target to verify against."),
    no_smoke_test: bool = typer.Option(
        False,
        "--no-smoke-test",
        help="Skip the per-destination `SELECT 1 LIMIT 1` queryability check.",
    ),
) -> None:
    """Verify the given target's state matches the latest build's manifest."""
    # Validate name shapes before any path or connection lookup. The
    # `target` value also flows into a Snowflake connection key, so a
    # malformed value would otherwise just fail at lookup time with a
    # less actionable error.
    try:
        validate_target_name(target)
        validate_artifact_name(name)
    except (InvalidTargetNameError, InvalidArtifactNameError) as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc

    project_dir = Path.cwd()

    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    repository = Repository(session_factory)

    try:
        exit_code = run_verify_command(
            pipeline_name=name,
            target=target,
            config=config,
            repository=repository,
            console=console,
            smoke_test=not no_smoke_test,
        )
    finally:
        engine.dispose()

    raise typer.Exit(code=exit_code)


def run_verify_command(
    *,
    pipeline_name: str,
    target: str,
    config: Config,
    repository: Repository,
    console: Console,
    smoke_test: bool = True,
    pool: SnowflakePool | None = None,
) -> int:
    """Run verify and return a process exit code.

    Pulled out of ``command`` so tests can drive it without the typer
    harness. Returns 0 on pass, non-zero on any failure.
    """
    # Defense-in-depth — tests and programmatic callers may bypass the
    # typer command and land here directly.
    try:
        validate_target_name(target)
        validate_artifact_name(pipeline_name)
    except (InvalidTargetNameError, InvalidArtifactNameError) as exc:
        console.print(f"[red]✗[/red] {exc}")
        return 2

    if target not in config.connections.snowflake:
        available = sorted(config.connections.snowflake.keys())
        console.print(
            f"[red]✗[/red] target {target!r} not defined in carve/connections.toml.\n"
            f"  Available targets: {available}"
        )
        return 2

    build = repository.latest_build_for(pipeline_name, target)
    if build is None:
        console.print(
            f"[red]✗[/red] No successful Build for pipeline {pipeline_name!r} in target {target!r}."
        )
        return 2

    plan_design = _load_plan_design(repository, build.plan_id)

    # Mirror `run_deploy`'s pool-lifetime pattern: only close pools we
    # constructed ourselves. Test callers and other programmatic
    # consumers pass their own `pool=` and remain responsible for it.
    owns_pool = pool is None
    pool = pool if pool is not None else SnowflakePool(config)

    try:
        try:
            runtime_conn = pool.get(target)
        except SnowflakeError as exc:
            console.print(f"[red]✗[/red] runtime connection unavailable: {exc}")
            return 2

        runtime_role: str | None = None
        runtime_section = config.connections.snowflake.get(target)
        if runtime_section is not None:
            runtime_role = runtime_section.role

        result = run_verify(
            runtime_connection=runtime_conn,
            build=build,
            plan_design=plan_design,
            runtime_role=runtime_role,
            smoke_test=smoke_test,
        )

        if result.ok:
            console.print(f"[green]✓[/green] {pipeline_name} verifies clean against {target!r}")
            return 0

        console.print(f"[red]✗[/red] verify failed for {pipeline_name} against {target!r}:")
        for failure in result.failures:
            console.print(f"  - {failure}")
        return 1
    finally:
        if owns_pool:
            pool.close_all()


def _load_plan_design(repository: Repository, plan_id: str) -> dict[str, Any] | None:
    plan: Plan | None = repository.get_plan(plan_id)
    if plan is None:
        return None
    # v0.1-01: task_graph_json is JSONB; ORM returns dict directly.
    task_graph = plan.task_graph_json
    if not isinstance(task_graph, dict):
        return None
    design = task_graph.get("design")
    return design if isinstance(design, dict) else None


__all__ = ["command", "run_verify_command"]
