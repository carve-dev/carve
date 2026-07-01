"""``carve serve`` — FastAPI + scheduler + reaper + archiver + worker pool.

The ``carve serve`` supervisor runs the REST API alongside the runtime loops
under one shutdown event (leader-election remains deferred). The co-resident
parts are:

* the **FastAPI app** (:func:`carve.api.main.create_app`) served by uvicorn on
  ``[api] host/port`` (default ``127.0.0.1:8765``) — plus the **webhook
  publisher** and an hourly **idempotency-key GC** that ride the same shutdown
  event;

and the four runtime parts under that same shutdown event:

* the **scheduler** loop — fires due schedules onto the job queue at each cron
  tick;
* the **reaper** loop — reclaims jobs from crashed/unreachable workers (a stale
  ``heartbeat_at``) so the queue's crash-recovery story is complete;
* the **archiver** loop — moves terminal rows older than each table's window from
  ``jobs``/``runs``/``logs``/``step_runs`` into their ``*_archive`` siblings
  (skippable with ``--no-archiver``);
* the **worker pool** — ``--workers N`` in-process workers that claim + run the
  jobs the scheduler queued (default 1; scale out further with ``carve worker``
  processes). Gated on the worker context; absent it, ``serve`` runs only the 3
  loops (the direct-``_serve`` unit-test path).

All four run as asyncio tasks under one shutdown ``asyncio.Event``. Ctrl-C /
SIGTERM sets it; the loops stop between their boundary sleeps and the pool
gracefully drains — each worker finishes its in-flight job, bounded by
``--grace-period``. A **second** Ctrl-C / SIGTERM sets a ``force`` Event that
cancels the still-running workers immediately, skipping the grace wait (the
interrupted job is left stale for the reaper).

Same setup block as ``carve worker``: ``load_config`` → resolve active target →
engine → ``initialize_database`` → session factory → :class:`JobQueue` +
:class:`Schedules` + :class:`Repository` + the shared :class:`EventEmitter`, plus
the :class:`WorkerContext` bundle the pool needs (``ProjectPaths``, connections,
the dbt executable, the ``on_run_failed`` hook). ``engine.dispose()`` runs in
``finally``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from carve.cli.orchestrator.extensibility_wiring import build_extensibility_on_run_failed_hook
from carve.core.config import ConfigError, load_config
from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import load_pipeline, resolve_required_label
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.store import StateStore
from carve.core.targets.resolution import resolve_active_target
from carve.runtime.archiver import DEFAULT_ARCHIVE_INTERVAL_S, archiver_loop
from carve.runtime.events import EventEmitter
from carve.runtime.reaper import DEFAULT_REAPER_INTERVAL_S, reaper_loop
from carve.runtime.scheduler import DEFAULT_INTERVAL_S, scheduler_loop
from carve.runtime.worker import WorkerContext, make_worker_id
from carve.runtime.worker_pool import DEFAULT_GRACE_PERIOD_S, run_worker_pool

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from sqlalchemy.orm import Session, sessionmaker

    from carve.core.config.schema import ArchiveConfig, ComponentConfig
    from carve.core.state.job_queue import JobQueue
    from carve.core.state.repository import Repository
    from carve.core.state.schedules import Schedules
    from carve.runtime.events import EventSink

console = Console()
logger = logging.getLogger(__name__)

# The creds-free dev substrate's dbt engine binary: a PATH lookup, matching
# ``carve worker``. Resolving a managed-venv dbt binary per target is a later
# runtime concern.
_DEFAULT_DBT_EXECUTABLE = "dbt"


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
        help="Skip the archiver loop (run only the scheduler + reaper + worker pool).",
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        help="In-process workers that claim + run queued jobs (default 1; "
        "scale out further with `carve worker` processes).",
    ),
    grace_period: float = typer.Option(
        DEFAULT_GRACE_PERIOD_S,
        "--drain-timeout",
        "--grace-period",
        help="Seconds to let in-flight jobs finish on shutdown before workers are "
        "cancelled (a second Ctrl-C / SIGTERM skips the wait).",
    ),
    label: str | None = typer.Option(
        None,
        "--label",
        help="Advertise a worker-placement label pool-wide. These workers then claim "
        "matching labeled jobs plus unlabeled ones; unset, they claim only unlabeled jobs.",
    ),
) -> None:
    """Run the Carve supervisor: FastAPI + scheduler + reaper + archiver + worker pool.

    The REST API (``[api] host/port``, default ``127.0.0.1:8765``) serves alongside
    the webhook publisher and idempotency-key GC; the scheduler fires due
    schedules; the reaper reclaims jobs from crashed / unreachable workers; the
    archiver moves aged-out terminal rows into the ``*_archive`` tables
    (``--no-archiver`` skips it); the worker pool (``--workers N``) claims and runs
    the queued jobs. Leader-election lands in a later runtime slice. Ctrl-C /
    SIGTERM stops every part and gracefully drains the pool (bounded by
    ``--grace-period``); a second signal cancels still-running workers immediately.
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
    # One emitter shared by every repo + the worker pool: the scheduler's
    # ``schedule.*``, the reaper's ``job.reclaimed`` (via the queue), the queue's
    # ``job.*``/``worker.*``, and the pool's ``run.*`` all persist durable
    # ``events`` rows through this single sink.
    # Lazy imports: keep FastAPI off the hot CLI-import path (every ``carve`` call
    # imports this module) — only ``serve`` pays the cost.
    from carve.api.auth import ensure_default_token
    from carve.api.main import create_app

    emitter = EventEmitter(session_factory)
    # One facade bundling every repo (old + the REST ``tokens``/``webhooks``/
    # ``idempotency_keys``) over the shared factory + emitter. The scheduler/reaper/
    # pool keep using its sub-repos; the FastAPI app + webhook publisher use it whole.
    state_store = StateStore(session_factory, emitter=emitter)
    job_queue = state_store.jobs
    schedules = state_store.schedules
    repository = state_store.repository

    project_paths = ProjectPaths.from_root(project_dir)
    # Bootstrap the OSS default token now that migrations are at head (the reliable
    # path — ``carve init`` may run before Postgres is up). Idempotent: a no-op once
    # a default token exists. Best-effort: never block startup on it.
    try:
        minted = ensure_default_token(state_store, project_paths.scratch_dir / "token")
        if minted is not None:
            console.print(
                "[green]serve[/green]: bootstrapped the default API token "
                "→ [bold].carve/token[/bold]."
            )
    except Exception:
        logger.warning("default API token bootstrap failed; continuing", exc_info=True)

    api_app = create_app(state_store, config, project_paths=project_paths)

    # The WorkerContext bundle the pool needs to turn jobs into persisted runs.
    # Lifted from ``carve worker``: the control-plane ``ProjectPaths``/connections,
    # the dbt executable, and the ``on_run_failed`` hook (a gated notify command,
    # ``None`` when no hooks.toml; gated at DEPLOY — the network floor).
    on_run_failed = build_extensibility_on_run_failed_hook(
        project_dir=project_dir,
        paths=config.paths,
    )
    worker_ctx = WorkerContext(
        repository=repository,
        job_queue=job_queue,
        paths=project_paths,
        connections=config.connections,
        dbt_executable=_DEFAULT_DBT_EXECUTABLE,
        worker_id=make_worker_id(),
        label=label,
        emitter=emitter,
        on_run_failed=on_run_failed,
    )

    def _resolve_label(pipeline_name: str) -> str | None:
        # The scheduler holds no ``ProjectPaths``/``components`` (the state store
        # stays config-agnostic), so ``carve serve`` — which does — supplies this
        # callback, delegating to the module-level, unit-tested helper below.
        return resolve_worker_label(
            pipeline_name, project_paths=project_paths, components=config.components
        )

    archiver_status = "off" if no_archiver else f"{archive_interval}s"
    console.print(
        f"[green]serve[/green]: API on {config.api.host}:{config.api.port} + "
        f"scheduler + reaper + archiver + worker pool ({workers}) "
        f"running for {active_target} "
        f"(scheduler {interval}s, reaper {reaper_interval}s, archiver {archiver_status}, "
        f"grace {grace_period}s; Ctrl-C to stop, twice to skip the drain)."
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
                worker_ctx=worker_ctx,
                workers=workers,
                grace_period_s=grace_period,
                resolve_label=_resolve_label,
                state_store=state_store,
                api_app=api_app,
                api_host=config.api.host,
                api_port=config.api.port,
                webhook_allow_private_ips=config.api.allow_private_webhook_ips,
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
    worker_ctx: WorkerContext | None = None,
    workers: int = 1,
    grace_period_s: float = DEFAULT_GRACE_PERIOD_S,
    force: asyncio.Event | None = None,
    resolve_label: Callable[[str], str | None] | None = None,
    state_store: StateStore | None = None,
    api_app: FastAPI | None = None,
    api_host: str = "127.0.0.1",
    api_port: int = 8765,
    webhook_allow_private_ips: bool = False,
) -> None:
    """Run scheduler + reaper (+ archiver + worker pool) until signalled, then stop.

    Every part shares ONE shutdown ``asyncio.Event``: the **first** SIGINT/SIGTERM
    sets it and the loops break between their boundary sleeps while the pool drains;
    a **second** signal sets the ``force`` Event so the pool cancels its still-running
    workers immediately. The stateful handler installs both; it falls back to
    ``KeyboardInterrupt`` where signal handlers can't be installed (e.g. a non-main
    thread under a test).

    The archiver task is created as a third ``tg.create_task`` only when
    ``run_archiver`` is set AND its ``session_factory``/``archive_config`` are
    supplied; the worker pool is created as a fourth ``tg.create_task`` only when a
    ``worker_ctx`` is supplied AND ``workers >= 1`` (so the direct-``_serve`` unit
    tests, which pass no ctx, keep running scheduler + reaper + archiver only). The
    pool is a single TaskGroup child sharing the shutdown Event, but it isolates its
    own N workers internally via ``gather`` — a crashed worker can't cancel the
    loops. The 3 daemon loops each swallow per-pass errors, so a fatal error in any
    one cancels the rest via the TaskGroup.
    """
    shutdown = asyncio.Event()
    force = force or asyncio.Event()

    def _on_signal() -> None:
        # First signal → graceful drain; second → skip the grace and cancel.
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
        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                scheduler_loop(
                    schedules,
                    job_queue,
                    interval_s=interval_s,
                    shutdown=shutdown,
                    resolve_label=resolve_label,
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
            if worker_ctx is not None and workers >= 1:
                tg.create_task(
                    run_worker_pool(
                        worker_ctx,
                        workers=workers,
                        shutdown=shutdown,
                        force=force,
                        grace_period_s=grace_period_s,
                    )
                )
            # The API server + webhook publisher + idempotency GC join the same
            # TaskGroup, sharing the one shutdown Event. Gated on ``api_app`` /
            # ``state_store`` so the direct-``_serve`` unit tests (which pass
            # neither) keep running only the runtime loops.
            if api_app is not None:
                tg.create_task(_run_api(api_app, api_host, api_port, shutdown))
            if state_store is not None:
                from carve.api.webhooks import webhook_publisher_loop

                tg.create_task(
                    webhook_publisher_loop(
                        state_store,
                        allow_private_ips=webhook_allow_private_ips,
                        shutdown=shutdown,
                    )
                )
                tg.create_task(_idempotency_gc_loop(state_store, shutdown=shutdown))
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)


async def _run_api(app: FastAPI, host: str, port: int, shutdown: asyncio.Event) -> None:
    """Serve the FastAPI app under uvicorn, stopping when ``shutdown`` is set.

    ``carve serve`` owns signal handling, so uvicorn's own handlers are disabled
    and shutdown is driven by flipping ``server.should_exit`` (uvicorn then drains
    in-flight requests and returns).
    """
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
    )
    server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
    serve_task = asyncio.create_task(server.serve())
    shutdown_task = asyncio.create_task(shutdown.wait())
    try:
        await asyncio.wait({serve_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True
        shutdown_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_task
        await serve_task


async def _idempotency_gc_loop(
    state_store: StateStore,
    *,
    shutdown: asyncio.Event,
    interval_s: float = 3600.0,
) -> None:
    """Delete expired idempotency-key rows hourly until ``shutdown`` is set."""
    while not shutdown.is_set():
        try:
            await asyncio.to_thread(state_store.idempotency_keys.delete_expired)
        except Exception:
            logger.warning("idempotency-key GC pass failed; continuing", exc_info=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval_s)


def resolve_worker_label(
    pipeline_name: str,
    *,
    project_paths: ProjectPaths,
    components: dict[str, ComponentConfig],
) -> str | None:
    """Reduce a due pipeline's component ``worker_label``s to its ``required_label``.

    Loads ``pipelines/<name>.toml`` and reduces its referenced components'
    ``worker_label``s to the single label the scheduled job must be placed on
    (``None`` = any worker). A per-pipeline failure (missing/unloadable file, a
    late-introduced label conflict) logs and returns ``None`` so one bad pipeline
    never starves the rest of a scheduler pass.

    **Path-confinement (security).** ``pipeline_name`` is ``schedule.pipeline`` —
    an unvalidated string a ``carve schedule set-cron`` / DB write controls — so a
    ``../`` or absolute value must not escape ``pipelines/`` and open+parse an
    arbitrary ``.toml``. Mirrors the guard in
    ``runtime/skills/pipeline_inspect.py``: the resolved file must sit directly
    under the resolved ``pipelines_dir``, or we return ``None`` (unlabeled)
    **without ever opening it**.
    """
    pipelines_dir = project_paths.pipelines_dir.resolve()
    toml_path = (pipelines_dir / f"{pipeline_name}.toml").resolve()
    if toml_path.parent != pipelines_dir:
        logger.warning(
            "pipeline name %r escapes the pipelines/ tree; treating as unlabeled",
            pipeline_name,
        )
        return None
    try:
        pipeline = load_pipeline(toml_path, components=components, paths=project_paths)
        return resolve_required_label(pipeline, components)
    except Exception:
        logger.warning(
            "could not resolve worker label for pipeline %r; treating as unlabeled",
            pipeline_name,
            exc_info=True,
        )
        return None


__all__ = ["command", "resolve_worker_label"]
