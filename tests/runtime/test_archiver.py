"""The archiver — verify-then-delete with no data loss, deterministic windows, emits.

Postgres-fixture-gated: the archiver runs raw ``INSERT … SELECT`` / ``DELETE``
against the real ``*_archive`` tables (migration 0011), so these exercise the
true SQL path. ``now`` is injected so the window / terminal-status filtering is
deterministic. Covers, per the runtime delivery spec's archiver bars:

* N terminal rows older than the window are archived; count matches; active rows
  deleted (``jobs`` end-to-end);
* an injected failure between INSERT and DELETE loses no data (active intact);
* a verification count-mismatch raises ``ArchiveVerificationFailed`` and halts the
  batch atomically (nothing deleted, nothing half-moved);
* window + terminal-status filtering keeps fresh / non-terminal rows;
* per-table predicate correctness (``jobs.finished_at`` / ``runs.completed_at`` /
  ``logs.timestamp`` age-only / ``step_runs.finished_at``);
* FK-safe ordering — children archived before their parent ``runs`` in one pass;
* ``archive_once`` emits ``archive.batch_completed`` per table with an injected
  ``EventSink``, and is a silent no-op without one;
* ``archiver_loop`` co-runs under a ``FakeClock`` and stops promptly on shutdown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from carve.core.config.schema import ArchiveConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Job, Log, Run, StepRun
from carve.runtime import archiver
from carve.runtime.archiver import (
    ArchiveVerificationFailed,
    archive_once,
    archive_table_safely,
    archiver_loop,
)
from carve.runtime.clock import FakeClock

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

# Terminal-status filters matching the archiver's per-table map (so the
# archive_table_safely unit tests drive the same predicate the loop does).
# Note the vocabulary split: jobs/step_runs use "succeeded"; ``runs`` uses the M1
# run vocabulary ("success"/"crashed"), NOT "succeeded".
_JOBS_TERMINAL = ("succeeded", "failed", "cancelled", "timed_out")
_RUNS_TERMINAL = ("success", "failed", "cancelled", "crashed")
_STEPS_TERMINAL = ("succeeded", "failed", "skipped")


@pytest.fixture
def session_factory(postgres_state_store_url: str) -> sessionmaker[Session]:
    config = _config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return create_session_factory(engine)


def _config(url: str) -> Any:
    from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig

    return Config(
        project=ProjectConfig(name="archiver-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=url),
    )


# --------------------------------------------------------------------------- helpers


def _count(factory: sessionmaker[Session], table: str) -> int:
    with factory() as session:
        return int(session.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())


def _ids(factory: sessionmaker[Session], table: str) -> list[str]:
    with factory() as session:
        return sorted(r[0] for r in session.execute(sa.text(f"SELECT id FROM {table}")).all())


def _seed_run(
    factory: sessionmaker[Session],
    *,
    run_id: str,
    status: str = "running",
    completed_at: datetime | None = None,
) -> None:
    with factory() as session:
        session.add(
            Run(id=run_id, kind="run", target_id="t", status=status, completed_at=completed_at)
        )
        session.commit()


# --------------------------------------------------------------------------- tests


def test_archive_once_moves_n_aged_terminal_jobs(session_factory: sessionmaker[Session]) -> None:
    """100 completed jobs older than the window are archived; counts match; deleted."""
    old = NOW - timedelta(days=10)  # > jobs_window (7d)
    with session_factory() as session:
        for i in range(100):
            session.add(
                Job(
                    id=f"job_{i}",
                    pipeline="sales",
                    target="dev",
                    status="succeeded",
                    finished_at=old,
                )
            )
        session.commit()

    moved = archive_once(session_factory, NOW, ArchiveConfig())

    assert moved["jobs"] == 100
    assert _count(session_factory, "jobs_archive") == 100
    assert _count(session_factory, "jobs") == 0


def test_injected_failure_between_insert_and_delete_loses_no_data(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure injected after INSERT, before DELETE → active table intact (no data loss).

    Under the single-transaction verify-then-delete the INSERT rolls back too, so
    the invariant asserted is *no row was lost* — every active row survives and
    the archive is empty (the batch is atomic).
    """
    old = NOW - timedelta(days=10)
    with session_factory() as session:
        for i in range(5):
            session.add(
                Job(
                    id=f"job_{i}",
                    pipeline="sales",
                    target="dev",
                    status="succeeded",
                    finished_at=old,
                )
            )
        session.commit()

    def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("disk full mid-delete")

    monkeypatch.setattr(archiver, "_archive_delete", _boom)

    with pytest.raises(RuntimeError, match="disk full"):
        archive_table_safely(
            session_factory,
            "jobs",
            cutoff=NOW - timedelta(days=7),
            status_filter=_JOBS_TERMINAL,
            finished_col="finished_at",
        )

    # No data loss: every active row still present; the archive insert rolled back.
    assert _count(session_factory, "jobs") == 5
    assert _count(session_factory, "jobs_archive") == 0


def test_verification_mismatch_raises_and_halts_atomically(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A count-mismatch raises ArchiveVerificationFailed before the DELETE — nothing moved."""
    old = NOW - timedelta(days=10)
    with session_factory() as session:
        for i in range(4):
            session.add(
                Job(
                    id=f"job_{i}",
                    pipeline="sales",
                    target="dev",
                    status="succeeded",
                    finished_at=old,
                )
            )
        session.commit()

    # Claim the insert moved a different number of rows than actually match — the
    # verify must catch it BEFORE any delete.
    monkeypatch.setattr(archiver, "_archive_insert", lambda *_a, **_k: 999)

    with pytest.raises(ArchiveVerificationFailed):
        archive_table_safely(
            session_factory,
            "jobs",
            cutoff=NOW - timedelta(days=7),
            status_filter=_JOBS_TERMINAL,
            finished_col="finished_at",
        )

    assert _count(session_factory, "jobs") == 4  # nothing deleted
    assert _count(session_factory, "jobs_archive") == 0  # nothing left half-moved


def test_window_and_status_filtering_excludes_fresh_and_nonterminal(
    session_factory: sessionmaker[Session],
) -> None:
    """Only ``finished_at < cutoff AND terminal`` moves; fresh / non-terminal stay."""
    old = NOW - timedelta(days=10)
    fresh = NOW - timedelta(days=1)
    with session_factory() as session:
        session.add(
            Job(id="old_done", pipeline="p1", target="dev", status="succeeded", finished_at=old)
        )
        # Fresh terminal — newer than the 7d cutoff, stays.
        session.add(
            Job(id="fresh_done", pipeline="p2", target="dev", status="succeeded", finished_at=fresh)
        )
        # Old but non-terminal — the status filter keeps it (even with an old finished_at).
        session.add(
            Job(id="old_running", pipeline="p3", target="dev", status="running", finished_at=old)
        )
        # Queued with NULL finished_at — the age predicate keeps it.
        session.add(Job(id="queued", pipeline="p4", target="dev", status="queued"))
        session.commit()

    moved = archive_once(session_factory, NOW, ArchiveConfig())

    assert moved["jobs"] == 1
    assert _ids(session_factory, "jobs_archive") == ["old_done"]
    assert _ids(session_factory, "jobs") == ["fresh_done", "old_running", "queued"]


def test_runs_predicate_uses_completed_at_and_real_terminal_vocabulary(
    session_factory: sessionmaker[Session],
) -> None:
    """``runs`` ages on ``completed_at`` + the REAL run vocab ("success"/"crashed").

    Regression test for the vocabulary bug: ``runs.status`` is "success", not
    "succeeded" (the worker maps it via ``_RUN_STATUS_BY_RESULT``), and a crash is
    "crashed". A filter of ``("succeeded", …)`` would archive *neither* the
    happy-path successes nor crashes — so this asserts a ``status="success"`` run
    IS archived (the assertion that catches the bug), plus a ``"crashed"`` one.
    """
    old = NOW - timedelta(days=40)  # > runs_window (30d)
    fresh = NOW - timedelta(days=1)
    with session_factory() as session:
        # Happy-path success (the majority) — MUST archive.
        session.add(
            Run(id="r_old_success", kind="run", target_id="t", status="success", completed_at=old)
        )
        # Crashed run — also terminal, MUST archive.
        session.add(
            Run(id="r_old_crashed", kind="run", target_id="t", status="crashed", completed_at=old)
        )
        # Non-terminal — stays (status filter).
        session.add(
            Run(id="r_old_running", kind="run", target_id="t", status="running", completed_at=old)
        )
        # Fresh success — stays (newer than the cutoff).
        session.add(
            Run(
                id="r_fresh_success",
                kind="run",
                target_id="t",
                status="success",
                completed_at=fresh,
            )
        )
        # Terminal but no completed_at — stays (age predicate).
        session.add(
            Run(id="r_null", kind="run", target_id="t", status="success", completed_at=None)
        )
        session.commit()

    moved = archive_table_safely(
        session_factory,
        "runs",
        cutoff=NOW - timedelta(days=30),
        status_filter=_RUNS_TERMINAL,
        finished_col="completed_at",
        tenant_scoped=False,  # runs predates multi-tenancy — no tenant_id column
    )

    assert moved == 2
    assert _ids(session_factory, "runs_archive") == ["r_old_crashed", "r_old_success"]
    assert _ids(session_factory, "runs") == ["r_fresh_success", "r_null", "r_old_running"]


def test_logs_predicate_is_age_only(session_factory: sessionmaker[Session]) -> None:
    """``logs`` archive by ``timestamp`` age alone — they carry no status."""
    _seed_run(session_factory, run_id="r1")  # parent stays; FK satisfied
    old = NOW - timedelta(days=40)
    fresh = NOW - timedelta(days=1)
    with session_factory() as session:
        session.add(Log(run_id="r1", timestamp=old, level="info", source="t", message="old"))
        session.add(Log(run_id="r1", timestamp=fresh, level="info", source="t", message="fresh"))
        session.commit()

    moved = archive_table_safely(
        session_factory,
        "logs",
        cutoff=NOW - timedelta(days=30),
        status_filter=None,
        finished_col="timestamp",
        tenant_scoped=False,  # logs has no tenant_id column
    )

    assert moved == 1
    with session_factory() as session:
        archived = [
            r[0] for r in session.execute(sa.text("SELECT message FROM logs_archive")).all()
        ]
        active = sorted(r[0] for r in session.execute(sa.text("SELECT message FROM logs")).all())
    assert archived == ["old"]
    assert active == ["fresh"]


def test_step_runs_predicate_uses_finished_at_and_terminal_status(
    session_factory: sessionmaker[Session],
) -> None:
    """``step_runs`` ages on ``finished_at`` + terminal status (incl. ``skipped``)."""
    _seed_run(session_factory, run_id="r1")
    old = NOW - timedelta(days=40)
    fresh = NOW - timedelta(days=1)
    with session_factory() as session:
        session.add(
            StepRun(
                id="sr_old_done",
                run_id="r1",
                step_id="a",
                step_type="sql",
                status="succeeded",
                finished_at=old,
            )
        )
        session.add(
            StepRun(
                id="sr_old_running",
                run_id="r1",
                step_id="b",
                step_type="sql",
                status="running",
                finished_at=old,
            )
        )
        session.add(
            StepRun(
                id="sr_fresh_done",
                run_id="r1",
                step_id="c",
                step_type="sql",
                status="succeeded",
                finished_at=fresh,
            )
        )
        session.commit()

    moved = archive_table_safely(
        session_factory,
        "step_runs",
        cutoff=NOW - timedelta(days=30),
        status_filter=_STEPS_TERMINAL,
        finished_col="finished_at",
        tenant_scoped=False,  # step_runs has no tenant_id column
    )

    assert moved == 1
    assert _ids(session_factory, "step_runs_archive") == ["sr_old_done"]


def test_archive_once_orders_children_before_parent_runs(
    session_factory: sessionmaker[Session],
) -> None:
    """A run + its job/log/step_run all age out and move in one pass — no FK violation.

    Active ``jobs``/``logs``/``step_runs`` FK -> ``runs.id``; the pass must delete
    the children before their parent run. If it didn't, the ``runs`` DELETE would
    trip the FK, archive_once would swallow it, and ``runs`` would be missing from
    the result.
    """
    job_old = NOW - timedelta(days=10)  # > jobs_window (7d)
    runs_old = NOW - timedelta(days=40)  # > runs/logs/steps window (30d)
    # Seed the parent run first (own transaction) so the children's FK resolves.
    # "success" is the real terminal runs vocab (NOT "succeeded"), so it archives.
    _seed_run(session_factory, run_id="r1", status="success", completed_at=runs_old)
    with session_factory() as session:
        session.add(
            Job(
                id="j1",
                pipeline="p",
                target="dev",
                status="succeeded",
                finished_at=job_old,
                run_id="r1",
            )
        )
        session.add(Log(run_id="r1", timestamp=runs_old, level="info", source="t", message="m"))
        session.add(
            StepRun(
                id="sr1",
                run_id="r1",
                step_id="a",
                step_type="sql",
                status="succeeded",
                finished_at=runs_old,
            )
        )
        session.commit()

    moved = archive_once(session_factory, NOW, ArchiveConfig())

    assert moved == {"jobs": 1, "step_runs": 1, "logs": 1, "runs": 1}
    for table in ("jobs", "runs", "logs", "step_runs"):
        assert _count(session_factory, table) == 0
        assert _count(session_factory, f"{table}_archive") == 1


class _SpySink:
    """Records every ``emit`` (the EventSink structural type)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, kind: str, payload: dict[str, Any], *, tenant_id: int = 1) -> None:
        self.events.append((kind, payload))


def test_archive_once_emits_batch_completed_per_table(
    session_factory: sessionmaker[Session],
) -> None:
    """Each table's batch emits ``archive.batch_completed`` (payload table/rows_moved)."""
    old = NOW - timedelta(days=10)
    with session_factory() as session:
        for i in range(3):
            session.add(
                Job(
                    id=f"job_{i}",
                    pipeline="sales",
                    target="dev",
                    status="succeeded",
                    finished_at=old,
                )
            )
        session.commit()

    sink = _SpySink()
    moved = archive_once(session_factory, NOW, ArchiveConfig(), emitter=sink)

    assert moved["jobs"] == 3
    assert [k for k, _ in sink.events] == ["archive.batch_completed"] * 4
    by_table = {p["table"]: p["rows_moved"] for _, p in sink.events}
    assert by_table == {"jobs": 3, "step_runs": 0, "logs": 0, "runs": 0}


def test_archive_once_without_emitter_is_silent(session_factory: sessionmaker[Session]) -> None:
    """No emitter (the back-compat default) → no emit, the pass still archives."""
    old = NOW - timedelta(days=10)
    with session_factory() as session:
        for i in range(2):
            session.add(
                Job(
                    id=f"job_{i}",
                    pipeline="sales",
                    target="dev",
                    status="succeeded",
                    finished_at=old,
                )
            )
        session.commit()

    moved = archive_once(session_factory, NOW, ArchiveConfig())  # emitter defaults to None

    assert moved["jobs"] == 2
    assert _count(session_factory, "jobs_archive") == 2


async def test_archiver_loop_runs_a_pass_then_stops_on_shutdown(
    session_factory: sessionmaker[Session],
) -> None:
    """The loop co-runs a pass under a FakeClock and stops promptly on shutdown."""
    old = NOW - timedelta(days=10)
    with session_factory() as session:
        for i in range(3):
            session.add(
                Job(
                    id=f"job_{i}",
                    pipeline="sales",
                    target="dev",
                    status="succeeded",
                    finished_at=old,
                )
            )
        session.commit()

    clock = FakeClock(NOW)
    shutdown = asyncio.Event()

    async def stop_after_archive() -> None:
        for _ in range(500):
            if _count(session_factory, "jobs_archive") >= 3:
                shutdown.set()
                return
            await asyncio.sleep(0)
        shutdown.set()

    await asyncio.wait_for(
        asyncio.gather(
            archiver_loop(
                session_factory, ArchiveConfig(), interval_s=30.0, clock=clock, shutdown=shutdown
            ),
            stop_after_archive(),
        ),
        timeout=5.0,
    )

    assert _count(session_factory, "jobs_archive") == 3
    assert _count(session_factory, "jobs") == 0


async def test_archiver_loop_stops_immediately_when_preset_shutdown(
    session_factory: sessionmaker[Session],
) -> None:
    """A pre-set shutdown exits the loop without spinning (bounded by a timeout)."""
    shutdown = asyncio.Event()
    shutdown.set()
    await asyncio.wait_for(
        archiver_loop(
            session_factory,
            ArchiveConfig(),
            interval_s=30.0,
            clock=FakeClock(NOW),
            shutdown=shutdown,
        ),
        timeout=2.0,
    )
