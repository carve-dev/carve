"""``carve worker`` — drain the job queue: claim a queued job, run it, persist.

The net-new runtime entry point. ``carve worker --once`` claims and runs a
single queued job (or exits cleanly on an empty queue); without ``--once`` it
loops, polling the queue on ``--poll-interval`` until interrupted (Ctrl-C).

It mirrors ``carve runs``' setup block — load ``Config``, build the engine,
``initialize_database``, construct the ``Repository`` + ``JobQueue`` — then
resolves the control-plane ``ProjectPaths``/connections and drives the worker
over the creds-free dlt→dbt→sql registry (``build_step_executor_registry``).

Scope (this slice): a single worker. ``--workers N`` (N > 1) is rejected with a
clear "single worker in this slice" message — the worker-pool fan-out rides a
later runtime slice. ``carve serve`` stays the existing stub.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from carve.core.config import ConfigError, load_config
from carve.core.config.paths import ProjectPaths
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.core.targets.resolution import resolve_active_target
from carve.runtime.worker import (
    DEFAULT_POLL_INTERVAL_S,
    WorkerContext,
    make_worker_id,
    run_once,
    worker_loop,
)

console = Console()

# The creds-free dev substrate's dbt engine binary: a PATH lookup, matching the
# registry/dev-run tests. Resolving a managed-venv dbt binary per target is the
# live-wiring concern of a later runtime slice.
_DEFAULT_DBT_EXECUTABLE = "dbt"


def command(
    once: bool = typer.Option(
        False,
        "--once",
        help="Claim and run a single queued job, then exit.",
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        help="Number of workers (only 1 is supported in this slice).",
    ),
    poll_interval: float = typer.Option(
        DEFAULT_POLL_INTERVAL_S,
        "--poll-interval",
        help="Seconds to wait between polls when the queue is empty (loop mode).",
    ),
) -> None:
    """Run a worker that drains the job queue."""
    if workers != 1:
        console.print(
            "[red]--workers > 1 is not supported in this slice[/red] "
            "(single worker only; the worker pool is a later runtime slice)."
        )
        raise typer.Exit(code=2)

    project_dir = Path.cwd()
    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    # Read the resolved ``--target`` flag at call time — the module-level slot
    # is mutated by ``main._main_callback`` after this module is imported, so a
    # module-level import would bind ``None``. (Same pattern as ``el``.)
    from carve.cli.main import ACTIVE_TARGET_FLAG

    active_target = resolve_active_target(ACTIVE_TARGET_FLAG, config)

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    repository = Repository(session_factory)
    job_queue = JobQueue(session_factory)

    ctx = WorkerContext(
        repository=repository,
        job_queue=job_queue,
        paths=ProjectPaths.from_root(project_dir),
        connections=config.connections,
        dbt_executable=_DEFAULT_DBT_EXECUTABLE,
        worker_id=make_worker_id(),
    )

    try:
        if once:
            ran = asyncio.run(run_once(ctx))
            if ran:
                console.print("[green]worker[/green]: ran one job.")
            else:
                console.print("[yellow]worker[/yellow]: queue empty, nothing to run.")
        else:
            console.print(
                f"[green]worker[/green]: polling for {active_target} jobs "
                f"(every {poll_interval}s; Ctrl-C to stop)."
            )
            asyncio.run(worker_loop(ctx, poll_interval_s=poll_interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]worker[/yellow]: shutting down.")
    finally:
        engine.dispose()

    raise typer.Exit(code=0)


__all__ = ["command"]
