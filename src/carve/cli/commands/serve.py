"""``carve serve`` — run the scheduler loop (scheduler-only, this slice).

The full ``carve serve`` SUPERVISOR (scheduler + reaper + archiver + worker pool
+ FastAPI) is deferred to later runtime slices. This slice ships a **minimal**
``carve serve`` that runs JUST the scheduler loop as a single asyncio task with
graceful shutdown on Ctrl-C / SIGTERM: it fires due schedules onto the job queue
(drained by ``carve worker``) at each cron tick.

Same setup block as ``carve worker``: ``load_config`` → resolve active target →
engine → ``initialize_database`` → session factory → :class:`JobQueue` +
:class:`Schedules`. ``engine.dispose()`` runs in ``finally``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path

import typer
from rich.console import Console

from carve.core.config import ConfigError, load_config
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.core.state.schedules import Schedules
from carve.core.targets.resolution import resolve_active_target
from carve.runtime.scheduler import DEFAULT_INTERVAL_S, scheduler_loop

console = Console()


def command(
    interval: float = typer.Option(
        DEFAULT_INTERVAL_S,
        "--interval",
        help="Scheduler poll interval in seconds (jobs fire within this of their cron time).",
    ),
) -> None:
    """Run the Carve scheduler loop (scheduler-only in this slice).

    The multi-loop supervisor (reaper, archiver, worker pool, API server) lands
    in a later runtime slice. Ctrl-C / SIGTERM stops the loop cleanly.
    """
    project_dir = Path.cwd()
    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    from carve.cli.main import ACTIVE_TARGET_FLAG

    active_target = resolve_active_target(ACTIVE_TARGET_FLAG, config)

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    job_queue = JobQueue(session_factory)
    schedules = Schedules(session_factory)

    console.print(
        f"[green]serve[/green]: scheduler running for {active_target} "
        f"(interval {interval}s; Ctrl-C to stop). "
        "[dim]scheduler-only this slice; the supervisor is deferred.[/dim]"
    )
    try:
        asyncio.run(_serve(schedules, job_queue, interval_s=interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]serve[/yellow]: shutting down.")
    finally:
        engine.dispose()

    raise typer.Exit(code=0)


async def _serve(
    schedules: Schedules,
    job_queue: JobQueue,
    *,
    interval_s: float,
) -> None:
    """Run ``scheduler_loop`` until SIGINT/SIGTERM, then stop it cleanly.

    Installs signal handlers that set the shutdown ``asyncio.Event`` (so the loop
    breaks between boundary sleeps); falls back to ``KeyboardInterrupt`` where
    signal handlers can't be installed (e.g. a non-main thread under a test).
    """
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, shutdown.set)
            installed.append(sig)
    try:
        await scheduler_loop(
            schedules,
            job_queue,
            interval_s=interval_s,
            shutdown=shutdown,
        )
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)


__all__ = ["command"]
