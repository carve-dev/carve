"""``carve serve`` — run the scheduler + reaper + archiver loops (three co-running loops).

The full ``carve serve`` SUPERVISOR (the above plus worker pool + FastAPI +
leader-election + graceful drain) is deferred to later runtime slices. This slice
runs THREE co-resident loops under one shutdown event:

* the **scheduler** loop — fires due schedules onto the job queue at each cron
  tick (drained by ``carve worker``);
* the **reaper** loop — reclaims jobs from crashed/unreachable workers (a stale
  ``heartbeat_at``) so the queue's crash-recovery story is complete;
* the **archiver** loop — moves terminal rows older than each table's window from
  ``jobs``/``runs``/``logs``/``step_runs`` into their ``*_archive`` siblings
  (skippable with ``--no-archiver``).

All three run as asyncio tasks under one shutdown ``asyncio.Event``; Ctrl-C /
SIGTERM sets it and they stop cleanly between their boundary sleeps. The worker
pool and API server remain deferred.

Same setup block as ``carve worker``: ``load_config`` → resolve active target →
engine → ``initialize_database`` → session factory → :class:`JobQueue` +
:class:`Schedules` + :class:`Repository` (the reaper needs the repository to fail
the orphaned in-flight runs) + the shared :class:`EventEmitter`. ``engine.dispose()``
runs in ``finally``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import TYPE_CHECKING

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
from carve.runtime.archiver import DEFAULT_ARCHIVE_INTERVAL_S, archiver_loop
from carve.runtime.events import EventEmitter
from carve.runtime.reaper import DEFAULT_REAPER_INTERVAL_S, reaper_loop
from carve.runtime.scheduler import DEFAULT_INTERVAL_S, scheduler_loop

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from carve.core.config.schema import ArchiveConfig
    from carve.runtime.events import EventSink

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
    archive_interval: float = typer.Option(
        DEFAULT_ARCHIVE_INTERVAL_S,
        "--archive-interval",
        help="Archiver poll interval in seconds (how often aged-out rows move to *_archive).",
    ),
    no_archiver: bool = typer.Option(
        False,
        "--no-archiver",
        help="Skip the archiver loop (run only the scheduler + reaper).",
    ),
) -> None:
    """Run the Carve scheduler + reaper + archiver loops (three co-running loops this slice).

    The scheduler fires due schedules; the reaper reclaims jobs from crashed /
    unreachable workers; the archiver moves aged-out terminal rows into the
    ``*_archive`` tables (``--no-archiver`` skips it). The rest of the multi-loop
    supervisor (worker pool, API server, leader-election, graceful drain) lands in
    a later runtime slice. Ctrl-C / SIGTERM stops every loop cleanly.
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
    # One emitter shared by both repos: the scheduler's ``schedule.*``/
    # ``schedule.skipped|fired`` and the reaper's ``job.reclaimed`` (via the
    # queue) all persist durable ``events`` rows.
    emitter = EventEmitter(session_factory)
    job_queue = JobQueue(session_factory, emitter=emitter)
    schedules = Schedules(session_factory, emitter=emitter)
    repository = Repository(session_factory)

    archiver_status = "off" if no_archiver else f"{archive_interval}s"
    console.print(
        f"[green]serve[/green]: scheduler + reaper + archiver running for {active_target} "
        f"(scheduler {interval}s, reaper {reaper_interval}s, archiver {archiver_status}; "
        "Ctrl-C to stop). "
        "[dim]scheduler + reaper + archiver this slice; the full supervisor is deferred.[/dim]"
    )
    try:
        asyncio.run(
            _serve(
                schedules,
                job_queue,
                repository,
                interval_s=interval,
                reaper_interval_s=reaper_interval,
                session_factory=session_factory,
                archive_config=config.runtime.archive,
                archive_interval_s=archive_interval,
                archive_emitter=emitter,
                run_archiver=not no_archiver,
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
    session_factory: sessionmaker[Session] | None = None,
    archive_config: ArchiveConfig | None = None,
    archive_interval_s: float = DEFAULT_ARCHIVE_INTERVAL_S,
    archive_emitter: EventSink | None = None,
    run_archiver: bool = True,
) -> None:
    """Run scheduler + reaper (+ archiver) until SIGINT/SIGTERM, then stop them all.

    Every loop shares ONE shutdown ``asyncio.Event``: a signal sets it and they
    break between their boundary sleeps. Installs signal handlers that set the
    event; falls back to ``KeyboardInterrupt`` where signal handlers can't be
    installed (e.g. a non-main thread under a test). The archiver task is created
    as a third ``tg.create_task`` only when ``run_archiver`` is set AND its
    ``session_factory``/``archive_config`` are supplied (so the helper degrades to
    scheduler + reaper when they aren't). Each loop already swallows per-pass
    errors, so ``asyncio.gather`` here surfaces only a fatal loop error — and a
    fatal error in one loop cancels the others.
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
            if run_archiver and session_factory is not None and archive_config is not None:
                tg.create_task(
                    archiver_loop(
                        session_factory,
                        archive_config,
                        interval_s=archive_interval_s,
                        emitter=archive_emitter,
                        shutdown=shutdown,
                    )
                )
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)


__all__ = ["command"]
