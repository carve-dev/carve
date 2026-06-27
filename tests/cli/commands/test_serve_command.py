"""``carve serve`` — scheduler + reaper co-run, starts both + shuts down cleanly.

The full multi-loop supervisor (archiver/worker pool/API/leader-election) is
deferred — this slice's ``carve serve`` runs the scheduler AND reaper loops under
one shutdown event with graceful stop. The tests assert the two-loop scope and a
clean stop (never hang: a pre-set shutdown event / a timeout bounds every wait).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.commands.serve import _serve
from carve.cli.main import app
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.core.state.repository import Repository
from carve.core.state.schedules import Schedules

runner = CliRunner()

_CARVE_TOML = """\
[project]
name = "serve-cli-test"
"""


def _handles(database_url: str) -> tuple[Schedules, JobQueue, Repository]:
    config = Config(
        project=ProjectConfig(name="serve-cli-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=database_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    factory = create_session_factory(engine)
    return Schedules(factory), JobQueue(factory), Repository(factory)


async def test_serve_runs_scheduler_and_reaper_and_stops_on_shutdown(
    postgres_state_store_url: str,
) -> None:
    """``_serve`` co-runs scheduler + reaper, then stops when shutdown is signalled.

    Drives ``_serve`` (the async core the command runs) directly with short
    intervals so the boundary sleeps are quick. A due schedule proves the
    scheduler ran; a planted stale job proves the reaper ran. Cancellation via a
    watchdog after both fire — bounded by a 5s timeout so a hang fails loudly
    rather than blocking the suite.
    """
    schedules, job_queue, repository = _handles(postgres_state_store_url)
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    # Force the schedule due right now so the first scheduler pass fires.
    from carve.core.state.models import Job, Schedule

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
        _serve(schedules, job_queue, repository, interval_s=0.05, reaper_interval_s=0.05)
    )

    async def watchdog() -> None:
        for _ in range(400):
            if fired and reclaimed:
                serve_task.cancel()
                return
            await asyncio.sleep(0.02)
        serve_task.cancel()

    await asyncio.gather(watchdog())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(serve_task, timeout=5.0)
    assert fired, "the scheduler loop should have fired the due schedule"
    assert reclaimed, "the reaper loop should have reclaimed the stale job"

    # The reaper actually reclaimed the stale job (queued, claim cleared).
    back = job_queue.get_job(stale.id)
    assert back is not None
    assert back.status == "queued"
    assert back.claimed_by is None


def test_serve_command_help_describes_scheduler_and_reaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command documents its scheduler + reaper scope and both options."""
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "scheduler" in result.output.lower()
    assert "reaper" in result.output.lower()
    assert "--interval" in result.output
    assert "--reaper-interval" in result.output


def test_serve_command_bad_config_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no carve.toml, ``carve serve`` exits 2 (the shared setup-block gate)."""
    monkeypatch.chdir(tmp_path)
    # No DATABASE_URL / carve.toml -> config resolution fails cleanly.
    result = runner.invoke(app, ["serve"], env={"CARVE_NO_DOTENV": "1"})
    assert result.exit_code == 2
