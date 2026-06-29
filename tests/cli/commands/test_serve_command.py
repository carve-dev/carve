"""``carve serve`` — scheduler + reaper + archiver co-run, start all + shut down cleanly.

The full multi-loop supervisor (worker pool/API/leader-election/drain) is still
deferred — this slice's ``carve serve`` runs the scheduler, reaper, AND archiver
loops under one shutdown event with graceful stop. The tests assert the
three-loop scope, that ``--no-archiver`` skips the archiver, and a clean stop
(never hang: a pre-set shutdown event / a timeout bounds every wait).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from carve.cli.commands.serve import _serve
from carve.cli.main import app
from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import (
    ArchiveConfig,
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.core.state.models import Job
from carve.core.state.repository import Repository
from carve.core.state.schedules import Schedules
from carve.runtime.worker import WorkerContext

runner = CliRunner()

# An archive window short enough that a freshly-finished job is already "aged
# out" for the serve tests (real-time finished_at minus a 1-second window).
_ARCHIVE_CONFIG = ArchiveConfig(
    jobs_window="1s", runs_window="1s", logs_window="1s", steps_window="1s"
)

_CARVE_TOML = """\
[project]
name = "serve-cli-test"
"""


def _handles(
    database_url: str,
) -> tuple[Schedules, JobQueue, Repository, sessionmaker[Session]]:
    config = Config(
        project=ProjectConfig(name="serve-cli-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=database_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    factory = create_session_factory(engine)
    return Schedules(factory), JobQueue(factory), Repository(factory), factory


def _seed_archivable_job(factory: sessionmaker[Session], job_id: str) -> None:
    """Insert a terminal job finished well in the past — archivable under a 1s window."""
    with factory() as session:
        session.add(
            Job(
                id=job_id,
                pipeline=f"done-{job_id}",
                target="dev",
                status="succeeded",
                finished_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        session.commit()


def _archive_count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(session.execute(sa.text("SELECT count(*) FROM jobs_archive")).scalar_one())


async def test_serve_runs_scheduler_reaper_and_archiver_and_stops_on_shutdown(
    postgres_state_store_url: str,
) -> None:
    """``_serve`` co-runs scheduler + reaper + archiver, then stops on shutdown.

    Drives ``_serve`` (the async core the command runs) directly with short
    intervals so the boundary sleeps are quick. A due schedule proves the
    scheduler ran; a planted stale job proves the reaper ran; an aged-out terminal
    job moved to ``jobs_archive`` proves the archiver ran. Cancellation via a
    watchdog after all three fire — bounded by a 5s timeout so a hang fails loudly
    rather than blocking the suite.
    """
    schedules, job_queue, repository, factory = _handles(postgres_state_store_url)
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    # Force the schedule due right now so the first scheduler pass fires.
    from carve.core.state.models import Schedule

    with schedules._session_factory() as session:
        row = session.get(Schedule, sched.id)
        assert row is not None
        row.next_fires_at = datetime(2020, 1, 1, tzinfo=UTC)
        session.commit()

    # Plant a STALE claimed job for a different pipeline so the reaper reclaims it.
    stale = job_queue.enqueue_scheduled("stuck", "dev")
    job_queue.claim_next("worker-dead")
    with job_queue._session_factory() as session:
        job_row = session.get(Job, stale.id)
        assert job_row is not None
        job_row.status = "running"
        job_row.claimed_by = "worker-dead"
        job_row.heartbeat_at = datetime(2020, 1, 1, tzinfo=UTC)  # ancient → stale
        session.commit()

    # Plant an aged-out terminal job so the archiver moves it to jobs_archive.
    _seed_archivable_job(factory, "archive_me")

    fired: list[bool] = []
    sched_original = schedules._emit

    def sched_spy(kind: str, payload: dict[str, object]) -> None:
        if kind == "schedule.fired":
            fired.append(True)
        sched_original(kind, payload)

    schedules._emit = sched_spy  # type: ignore[method-assign]

    reclaimed: list[bool] = []
    queue_original = job_queue._emit

    def queue_spy(kind: str, payload: dict[str, object]) -> None:
        if kind == "job.reclaimed":
            reclaimed.append(True)
        queue_original(kind, payload)

    job_queue._emit = queue_spy  # type: ignore[method-assign]

    serve_task = asyncio.create_task(
        _serve(
            schedules,
            job_queue,
            repository,
            interval_s=0.05,
            reaper_interval_s=0.05,
            session_factory=factory,
            archive_config=_ARCHIVE_CONFIG,
            archive_interval_s=0.05,
            run_archiver=True,
        )
    )

    async def watchdog() -> None:
        for _ in range(400):
            if fired and reclaimed and _archive_count(factory) >= 1:
                serve_task.cancel()
                return
            await asyncio.sleep(0.02)
        serve_task.cancel()

    await asyncio.gather(watchdog())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(serve_task, timeout=5.0)
    assert fired, "the scheduler loop should have fired the due schedule"
    assert reclaimed, "the reaper loop should have reclaimed the stale job"
    assert _archive_count(factory) >= 1, "the archiver loop should have archived the aged job"

    # The reaper actually reclaimed the stale job (queued, claim cleared).
    back = job_queue.get_job(stale.id)
    assert back is not None
    assert back.status == "queued"
    assert back.claimed_by is None
    # The archiver actually moved the aged terminal job out of the active table.
    assert job_queue.get_job("archive_me") is None


async def test_serve_no_archiver_skips_the_archiver_loop(
    postgres_state_store_url: str,
) -> None:
    """``run_archiver=False`` (the ``--no-archiver`` path) leaves aged rows in place.

    Same setup, but the archiver task is never created — the aged terminal job
    stays in the active table, and ``jobs_archive`` stays empty, while the
    scheduler + reaper still run and stop cleanly.
    """
    schedules, job_queue, repository, factory = _handles(postgres_state_store_url)
    _seed_archivable_job(factory, "archive_me")

    serve_task = asyncio.create_task(
        _serve(
            schedules,
            job_queue,
            repository,
            interval_s=0.05,
            reaper_interval_s=0.05,
            session_factory=factory,
            archive_config=_ARCHIVE_CONFIG,
            archive_interval_s=0.05,
            run_archiver=False,
        )
    )

    # Let several archive intervals elapse; with the loop skipped nothing moves.
    await asyncio.sleep(0.4)
    serve_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(serve_task, timeout=5.0)

    assert _archive_count(factory) == 0
    assert job_queue.get_job("archive_me") is not None


async def test_serve_runs_worker_pool_alongside_loops_and_stops_on_shutdown(
    postgres_state_store_url: str, tmp_path: Path
) -> None:
    """``_serve(..., worker_ctx=…, workers=2)`` runs the pool + 3 loops, stops all.

    With a ``worker_ctx`` supplied, ``_serve`` adds the worker pool as a fourth
    TaskGroup child. The two ``serve-pool:task{i}`` worker rows registering prove
    the pool runs alongside the scheduler + reaper + archiver; cancelling ``_serve``
    drains the pool so both rows end ``stopped`` (unregistered). Bounded by a 5s
    watchdog + timeout so a drain bug fails loudly.
    """
    schedules, job_queue, repository, factory = _handles(postgres_state_store_url)
    worker_ctx = WorkerContext(
        repository=repository,
        job_queue=job_queue,
        paths=ProjectPaths.from_root(tmp_path),
        connections=ConnectionsConfig(),
        dbt_executable="dbt",
        worker_id="serve-pool",
    )

    serve_task = asyncio.create_task(
        _serve(
            schedules,
            job_queue,
            repository,
            interval_s=0.05,
            reaper_interval_s=0.05,
            session_factory=factory,
            archive_config=_ARCHIVE_CONFIG,
            archive_interval_s=0.05,
            run_archiver=True,
            worker_ctx=worker_ctx,
            workers=2,
            grace_period_s=5.0,
        )
    )

    registered = False
    for _ in range(250):
        if all(job_queue.get_worker(f"serve-pool:task{i}") is not None for i in range(2)):
            registered = True
            break
        await asyncio.sleep(0.02)
    serve_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(serve_task, timeout=5.0)

    assert registered, "the worker pool did not register its workers alongside the loops"
    for i in range(2):
        worker = job_queue.get_worker(f"serve-pool:task{i}")
        assert worker is not None
        assert worker.status == "stopped", f"task{i} was not drained/unregistered on shutdown"


def test_serve_command_help_describes_all_loops_and_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command documents its scheduler + reaper + archiver + worker-pool scope."""
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "scheduler" in result.output.lower()
    assert "reaper" in result.output.lower()
    assert "archiver" in result.output.lower()
    assert "--interval" in result.output
    assert "--reaper-interval" in result.output
    assert "--archive-interval" in result.output
    assert "--no-archiver" in result.output
    assert "--workers" in result.output
    assert "--drain-timeout" in result.output


def test_serve_command_bad_config_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no carve.toml, ``carve serve`` exits 2 (the shared setup-block gate)."""
    monkeypatch.chdir(tmp_path)
    # No DATABASE_URL / carve.toml -> config resolution fails cleanly.
    result = runner.invoke(app, ["serve"], env={"CARVE_NO_DOTENV": "1"})
    assert result.exit_code == 2
