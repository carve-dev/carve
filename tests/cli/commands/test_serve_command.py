"""Minimal ``carve serve`` — scheduler-only, starts the loop + shuts down cleanly.

The full multi-loop supervisor (reaper/archiver/worker pool/API) is deferred —
this slice's ``carve serve`` runs JUST the scheduler loop with graceful shutdown.
The tests assert the scheduler-only scope and a clean stop (never hang: a pre-set
shutdown event / a timeout bounds every wait).
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
from carve.core.state.schedules import Schedules

runner = CliRunner()

_CARVE_TOML = """\
[project]
name = "serve-cli-test"
"""


def _handles(database_url: str) -> tuple[Schedules, JobQueue]:
    config = Config(
        project=ProjectConfig(name="serve-cli-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=database_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    factory = create_session_factory(engine)
    return Schedules(factory), JobQueue(factory)


async def test_serve_runs_scheduler_loop_and_stops_on_shutdown(
    postgres_state_store_url: str,
) -> None:
    """``_serve`` fires a due schedule, then stops when shutdown is signalled.

    Drives ``_serve`` (the async core the command runs) directly with a real
    interval so the boundary sleep is short, and cancels via a watchdog after the
    fire lands — bounded by a 5s timeout so a hang fails loudly rather than
    blocking the suite.
    """
    schedules, job_queue = _handles(postgres_state_store_url)
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    # Force it due right now so the first pass fires.
    from carve.core.state.models import Schedule

    with schedules._session_factory() as session:
        row = session.get(Schedule, sched.id)
        assert row is not None
        row.next_fires_at = datetime(2020, 1, 1, tzinfo=UTC)
        session.commit()

    fired: list[bool] = []
    original = schedules._emit

    def spy(kind: str, payload: dict[str, object]) -> None:
        if kind == "schedule.fired":
            fired.append(True)
        original(kind, payload)

    schedules._emit = spy  # type: ignore[method-assign]

    serve_task = asyncio.create_task(_serve(schedules, job_queue, interval_s=0.05))

    async def watchdog() -> None:
        for _ in range(200):
            if fired:
                serve_task.cancel()
                return
            await asyncio.sleep(0.02)
        serve_task.cancel()

    await asyncio.gather(watchdog())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(serve_task, timeout=5.0)
    assert fired, "the scheduler-only serve loop should have fired the due schedule"


def test_serve_command_help_describes_scheduler_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command exists and documents its scheduler-only scope (no supervisor)."""
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "scheduler" in result.output.lower()
    assert "--interval" in result.output


def test_serve_command_bad_config_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no carve.toml, ``carve serve`` exits 2 (the shared setup-block gate)."""
    monkeypatch.chdir(tmp_path)
    # No DATABASE_URL / carve.toml -> config resolution fails cleanly.
    result = runner.invoke(app, ["serve"], env={"CARVE_NO_DOTENV": "1"})
    assert result.exit_code == 2
