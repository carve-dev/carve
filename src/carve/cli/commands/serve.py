"""``carve serve`` — run the scheduler + reaper loops (two co-running loops).

The full ``carve serve`` SUPERVISOR (scheduler + reaper + archiver + worker pool
+ FastAPI + leader-election) is deferred to later runtime slices. This slice runs
TWO co-resident loops under one shutdown event:

* the **scheduler** loop — fires due schedules onto the job queue at each cron
  tick (drained by ``carve worker``);
* the **reaper** loop — reclaims jobs from crashed/unreachable workers (a stale
  ``heartbeat_at``) so the queue's crash-recovery story is complete.

Both run as asyncio tasks under one shutdown ``asyncio.Event``; Ctrl-C / SIGTERM
sets it and both stop cleanly between their boundary sleeps. The worker pool,
archiver, and API server remain deferred.

Same setup block as ``carve worker``: ``load_config`` → resolve active target →
engine → ``initialize_database`` → session factory → :class:`JobQueue` +
:class:`Schedules` + :class:`Repository` (the reaper needs the repository to fail
the orphaned in-flight runs). ``engine.dispose()`` runs in ``finally``.
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
from carve.core.state.repository import Repository
from carve.core.state.schedules import Schedules
from carve.core.targets.resolution import resolve_active_target
from carve.runtime.reaper import DEFAULT_REAPER_INTERVAL_S, reaper_loop
from carve.runtime.scheduler import DEFAULT_INTERVAL_S, scheduler_loop

console = Console()


def command(
    interval: float = typer.Option(
        DEFAULT_INTERVAL_S,
        "--interval",
        help="Scheduler poll interval in seconds (jobs fire within this of their cron time).",
    ),
    reaper_interval: float = typer.Option(
        DEFAULT_REAPER_INTERVAL_S,
        "--reaper-interval",
        help="Reaper poll interval in seconds (how often stale claims are reclaimed).",
    ),
) -> None:
    """Run the Carve scheduler + reaper loops (two co-running loops this slice).

    The scheduler fires due schedules; the reaper reclaims jobs from crashed /
    unreachable workers. The rest of the multi-loop supervisor (archiver, worker
    pool, API server, leader-election) lands in a later runtime slice. Ctrl-C /
    SIGTERM stops both loops cleanly.
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
    repository = Repository(session_factory)

    console.print(
        f"[green]serve[/green]: scheduler + reaper running for {active_target} "
        f"(scheduler {interval}s, reaper {reaper_interval}s; Ctrl-C to stop). "
        "[dim]scheduler + reaper this slice; the full supervisor is deferred.[/dim]"
    )
    try:
        asyncio.run(
            _serve(
                schedules,
                job_queue,
                repository,
                interval_s=interval,
                reaper_interval_s=reaper_interval,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]serve[/yellow]: shutting down.")
    finally:
        engine.dispose()

    raise typer.Exit(code=0)


async def _serve(
    schedules: Schedules,
    job_queue: JobQueue,
    repository: Repository,
    *,
    interval_s: float,
    reaper_interval_s: float = DEFAULT_REAPER_INTERVAL_S,
) -> None:
    """Run ``scheduler_loop`` + ``reaper_loop`` until SIGINT/SIGTERM, then stop both.

    Both loops share ONE shutdown ``asyncio.Event``: a signal sets it and both
    break between their boundary sleeps. Installs signal handlers that set the
    event; falls back to ``KeyboardInterrupt`` where signal handlers can't be
    installed (e.g. a non-main thread under a test). Each loop already swallows
    per-pass errors, so ``asyncio.gather`` here surfaces only a fatal loop error
    (e.g. a programming bug) — and a fatal error in one loop cancels the other.
    """
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, shutdown.set)
            installed.append(sig)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                scheduler_loop(
                    schedules,
                    job_queue,
                    interval_s=interval_s,
                    shutdown=shutdown,
                )
            )
            tg.create_task(
                reaper_loop(
                    job_queue,
                    repository,
                    interval_s=reaper_interval_s,
                    shutdown=shutdown,
                )
            )
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)


__all__ = ["command"]
