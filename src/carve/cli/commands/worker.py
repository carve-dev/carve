"""``carve worker`` — drain the job queue: claim a queued job, run it, persist.

The net-new runtime entry point. ``carve worker --once`` claims and runs a
single queued job (or exits cleanly on an empty queue); without ``--once`` it
runs a pool of ``--workers N`` (default 1) in-process workers polling the queue on
``--poll-interval`` until interrupted (Ctrl-C). One base ``WorkerContext`` (a
single session pool) is shared across the N tasks — the queue is the only
coordination point.

It mirrors ``carve runs``' setup block — load ``Config``, build the engine,
``initialize_database``, construct the ``Repository`` + ``JobQueue`` — then
resolves the control-plane ``ProjectPaths``/connections and drives the worker(s)
over the creds-free dlt→dbt→sql registry (``build_step_executor_registry``).

Shutdown: the first Ctrl-C / SIGTERM gracefully drains (each worker finishes its
in-flight job); a second cancels the still-running workers immediately (the
interrupted job is left stale for the reaper).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path

import typer
from rich.console import Console

from carve.cli.orchestrator.extensibility_wiring import build_extensibility_on_run_failed_hook
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
from carve.runtime.events import EventEmitter
from carve.runtime.worker import (
    DEFAULT_POLL_INTERVAL_S,
    WorkerContext,
    make_worker_id,
    run_once,
)
from carve.runtime.worker_pool import DEFAULT_GRACE_PERIOD_S, run_worker_pool

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
        help="Number of in-process workers to fan the pool out to (loop mode).",
    ),
    poll_interval: float = typer.Option(
        DEFAULT_POLL_INTERVAL_S,
        "--poll-interval",
        help="Seconds to wait between polls when the queue is empty (loop mode).",
    ),
    label: str | None = typer.Option(
        None,
        "--label",
        help="Advertise a worker-placement label. This worker then claims matching "
        "labeled jobs plus unlabeled ones; unset, it claims only unlabeled jobs.",
    ),
) -> None:
    """Run a worker (or a pool of workers) that drains the job queue."""
    if workers < 1:
        console.print("[red]--workers must be >= 1[/red].")
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
    emitter = EventEmitter(session_factory)
    repository = Repository(session_factory)
    job_queue = JobQueue(session_factory, emitter=emitter)

    # The user ``on_run_failed`` hook (a gated notify command) — fired at the
    # worker's ``run.failed`` transition, independently of the durable event. A
    # missing/empty hooks.toml yields ``None`` (no hook); a malformed one is
    # fail-closed (raises before any work). Gated at DEPLOY (the network floor —
    # see ``build_extensibility_on_run_failed_hook``).
    on_run_failed = build_extensibility_on_run_failed_hook(
        project_dir=project_dir,
        paths=config.paths,
    )

    ctx = WorkerContext(
        repository=repository,
        job_queue=job_queue,
        paths=ProjectPaths.from_root(project_dir),
        connections=config.connections,
        dbt_executable=_DEFAULT_DBT_EXECUTABLE,
        worker_id=make_worker_id(),
        label=label,
        emitter=emitter,
        on_run_failed=on_run_failed,
    )

    try:
        if once:
            # The single-job, single-worker path: one ``run_once``, no pool.
            ran = asyncio.run(run_once(ctx))
            if ran:
                console.print("[green]worker[/green]: ran one job.")
            else:
                console.print("[yellow]worker[/yellow]: queue empty, nothing to run.")
        else:
            console.print(
                f"[green]worker[/green]: pool of {workers} polling for {active_target} jobs "
                f"(every {poll_interval}s; Ctrl-C to stop, twice to skip the drain)."
            )
            asyncio.run(_run_pool(ctx, workers=workers, poll_interval_s=poll_interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]worker[/yellow]: shutting down.")
    finally:
        engine.dispose()

    raise typer.Exit(code=0)


async def _run_pool(
    ctx: WorkerContext,
    *,
    workers: int,
    poll_interval_s: float,
    grace_period_s: float = DEFAULT_GRACE_PERIOD_S,
) -> None:
    """Drive :func:`run_worker_pool`, wiring Ctrl-C / SIGTERM to the drain.

    Installs the same stateful signal handler as ``carve serve``: the first signal
    sets ``shutdown`` (graceful drain — each worker finishes its in-flight job); a
    second sets ``force`` (cancel the stragglers, skip the grace). Falls back to
    ``KeyboardInterrupt`` where signal handlers can't be installed (e.g. a non-main
    thread under a test).
    """
    shutdown = asyncio.Event()
    force = asyncio.Event()

    def _on_signal() -> None:
        if shutdown.is_set():
            force.set()
        else:
            shutdown.set()

    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, _on_signal)
            installed.append(sig)
    try:
        await run_worker_pool(
            ctx,
            workers=workers,
            shutdown=shutdown,
            force=force,
            grace_period_s=grace_period_s,
            poll_interval_s=poll_interval_s,
        )
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)


__all__ = ["command"]
