"""The worker-pool fan-out + graceful-drain slice, in-process + deterministic.

The spec's "spawn N processes / kill -9" integration cases collapse to N
``worker_loop`` coroutines sharing one Postgres + the queue's
``expected_worker_id`` ownership guard — no real subprocesses, no wall-clock
sleeps, and a stubbed ``_execute_job`` so the queue fan-out is what's under test
(not step execution, covered by ``test_worker_end_to_end``). Every wait is
timeout-bounded so a drain bug fails loudly instead of hanging the suite.

Coverage:

* **none-twice fan-out** — N workers (unique ``:taskN`` ids) drain M queued jobs;
  every job is terminal exactly once (M distinct runs) and the N worker rows are
  registered then unregistered.
* **graceful drain waits for in-flight** — a worker mid-``run_once`` finishes its
  job after ``shutdown`` is set; the pool then exits + unregisters within grace.
* **second signal skips grace** — ``force`` cancels the worker mid-run; the pool
  exits promptly, the in-flight job is left non-terminal for the reaper, the
  worker still unregisters.
* **per-task crash isolation** — a worker that escapes its guard is logged +
  dropped; its siblings keep draining and the pool still completes (proving
  ``gather(return_exceptions=True)``, not a bare ``TaskGroup``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

import carve.runtime.worker as worker_mod
from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import (
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
from carve.core.state.repository import Repository
from carve.runtime.execute_pipeline import RunResult
from carve.runtime.worker import WorkerContext
from carve.runtime.worker_pool import run_worker_pool

if TYPE_CHECKING:
    from pathlib import Path

_TERMINAL = {"succeeded", "failed"}


def _ok_result() -> RunResult:
    return RunResult(
        status="succeeded",
        completed=frozenset({"only"}),
        failed=frozenset(),
        skipped=frozenset(),
    )


async def _succeed_execute(
    c: WorkerContext, *, job_pipeline: str, run_id: str, target: str
) -> RunResult:
    """A stub ``_execute_job`` that succeeds instantly (no real steps)."""
    return _ok_result()


@pytest.fixture
def pool_ctx(tmp_path: Path, postgres_state_store_url: str) -> WorkerContext:
    """A base ``WorkerContext`` over a fresh Postgres, base ``worker_id='pool'``.

    The fan-out gives each task a ``pool:task{i}`` id. ``_execute_job`` is stubbed
    per-test, so the (unused) ``paths``/``connections``/registry are minimal.
    """
    config = Config(
        project=ProjectConfig(name="pool-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    factory = create_session_factory(engine)
    return WorkerContext(
        repository=Repository(factory),
        job_queue=JobQueue(factory),
        paths=ProjectPaths.from_root(tmp_path),
        connections=ConnectionsConfig(),
        dbt_executable="dbt",
        worker_id="pool",
    )


def _run_count(queue: JobQueue) -> int:
    with queue._session_factory() as session:
        return int(session.execute(sa.text("SELECT count(*) FROM runs")).scalar_one())


async def _wait_until(predicate: Any, *, attempts: int = 500, delay: float = 0.02) -> bool:
    """Poll ``predicate`` until true or ``attempts`` exhausted (timeout-bounded)."""
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(delay)
    return False


async def test_pool_drains_every_job_exactly_once(
    pool_ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N workers drain M distinct-pipeline jobs; each terminal once; rows cycle."""
    monkeypatch.setattr(worker_mod, "_execute_job", _succeed_execute)
    queue = pool_ctx.job_queue
    n_workers, m_jobs = 3, 5
    job_ids = [queue.enqueue_manual(f"p{i}", "dev", trigger="manual").id for i in range(m_jobs)]

    shutdown = asyncio.Event()
    pool = asyncio.create_task(
        run_worker_pool(
            pool_ctx,
            workers=n_workers,
            shutdown=shutdown,
            grace_period_s=5.0,
            poll_interval_s=0.02,
        )
    )

    drained = await _wait_until(
        lambda: all(
            (j := queue.get_job(jid)) is not None and j.status in _TERMINAL for jid in job_ids
        )
    )
    shutdown.set()
    await asyncio.wait_for(pool, timeout=5.0)
    assert drained, "the pool did not drain every queued job"

    # Every job is terminal succeeded, and there is exactly one run per job — so no
    # job was ever claimed + run twice.
    for jid in job_ids:
        job = queue.get_job(jid)
        assert job is not None
        assert job.status == "succeeded"
        assert job.run_id is not None
    assert _run_count(queue) == m_jobs

    # All N worker rows were registered then unregistered (stopped) on clean exit.
    for i in range(n_workers):
        worker = queue.get_worker(f"pool:task{i}")
        assert worker is not None, f"task{i} never registered"
        assert worker.status == "stopped"


async def test_graceful_drain_waits_for_in_flight_job(
    pool_ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting ``shutdown`` lets a mid-flight ``run_once`` complete, then the pool exits."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_execute(
        c: WorkerContext, *, job_pipeline: str, run_id: str, target: str
    ) -> RunResult:
        started.set()
        await release.wait()
        return _ok_result()

    monkeypatch.setattr(worker_mod, "_execute_job", _blocking_execute)
    queue = pool_ctx.job_queue
    job = queue.enqueue_manual("slow", "dev", trigger="manual")

    shutdown = asyncio.Event()
    pool = asyncio.create_task(
        run_worker_pool(
            pool_ctx,
            workers=1,
            shutdown=shutdown,
            grace_period_s=5.0,
            poll_interval_s=0.02,
        )
    )

    assert await asyncio.wait_for(started.wait(), timeout=5.0)
    # The job is in-flight (running), not yet terminal.
    in_flight = queue.get_job(job.id)
    assert in_flight is not None
    assert in_flight.status == "running"

    # Drain requested: the in-flight job must still complete (not be cancelled).
    shutdown.set()
    await asyncio.sleep(0.05)
    still = queue.get_job(job.id)
    assert still is not None
    assert still.status == "running", "drain cancelled the in-flight job instead of waiting"

    # Let the in-flight job finish; the pool drains and exits within grace.
    release.set()
    await asyncio.wait_for(pool, timeout=5.0)

    finished = queue.get_job(job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    worker = queue.get_worker("pool:task0")
    assert worker is not None
    assert worker.status == "stopped"


async def test_second_signal_skips_grace_and_cancels(
    pool_ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``force`` cancels a worker mid-run; the pool exits well under the grace period."""
    started = asyncio.Event()
    never = asyncio.Event()  # deliberately never set

    async def _never_returns(
        c: WorkerContext, *, job_pipeline: str, run_id: str, target: str
    ) -> RunResult:
        started.set()
        await never.wait()
        return _ok_result()  # pragma: no cover - never reached

    monkeypatch.setattr(worker_mod, "_execute_job", _never_returns)
    queue = pool_ctx.job_queue
    job = queue.enqueue_manual("hang", "dev", trigger="manual")

    shutdown = asyncio.Event()
    force = asyncio.Event()
    pool = asyncio.create_task(
        run_worker_pool(
            pool_ctx,
            workers=1,
            shutdown=shutdown,
            force=force,
            grace_period_s=100.0,  # huge: only force can make this exit promptly
            poll_interval_s=0.02,
        )
    )

    assert await asyncio.wait_for(started.wait(), timeout=5.0)
    shutdown.set()
    force.set()

    # The second signal skips the (100s) grace: the pool returns promptly.
    await asyncio.wait_for(pool, timeout=5.0)

    # The interrupted in-flight job is left non-terminal for the reaper to reclaim.
    left = queue.get_job(job.id)
    assert left is not None
    assert left.status == "running"
    # The cancelled worker still unregistered in its ``finally``.
    worker = queue.get_worker("pool:task0")
    assert worker is not None
    assert worker.status == "stopped"


async def test_one_worker_crash_does_not_take_down_siblings(
    pool_ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that escapes its guard is logged + dropped; siblings keep draining."""
    monkeypatch.setattr(worker_mod, "_execute_job", _succeed_execute)
    queue = pool_ctx.job_queue

    # Capture the pool's crash log via a handler attached directly to the module
    # logger (not `caplog`) so the assertion is immune to cross-test
    # log-propagation / `disable_existing_loggers` state (the review-wiring tests'
    # precedent).
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    plogger = logging.getLogger("carve.runtime.worker_pool")
    handler = _Capture()
    plogger.addHandler(handler)
    plogger.setLevel(logging.ERROR)
    plogger.disabled = False

    # Make exactly the ``:task0`` worker crash *outside* ``run_once``'s catch — a
    # ``register_worker`` raise escapes ``worker_loop`` entirely (it runs before the
    # try/finally), the strongest test of crash isolation.
    original_register = queue.register_worker

    def crashing_register(worker_id: str, **kwargs: Any) -> Any:
        if worker_id.endswith(":task0"):
            raise RuntimeError("worker task0 register exploded")
        return original_register(worker_id, **kwargs)

    monkeypatch.setattr(queue, "register_worker", crashing_register)

    n_workers, m_jobs = 3, 4
    job_ids = [queue.enqueue_manual(f"q{i}", "dev", trigger="manual").id for i in range(m_jobs)]

    shutdown = asyncio.Event()
    try:
        pool = asyncio.create_task(
            run_worker_pool(
                pool_ctx,
                workers=n_workers,
                shutdown=shutdown,
                grace_period_s=5.0,
                poll_interval_s=0.02,
            )
        )

        drained = await _wait_until(
            lambda: all(
                (j := queue.get_job(jid)) is not None and j.status in _TERMINAL for jid in job_ids
            )
        )
        shutdown.set()
        # The pool completes without raising despite the crashed sibling.
        await asyncio.wait_for(pool, timeout=5.0)
    finally:
        plogger.removeHandler(handler)

    assert drained, "the surviving workers did not drain every job"
    for jid in job_ids:
        job = queue.get_job(jid)
        assert job is not None
        assert job.status == "succeeded"
    assert _run_count(queue) == m_jobs

    # The crashed worker never registered; its siblings registered + stopped.
    assert queue.get_worker("pool:task0") is None
    for i in (1, 2):
        worker = queue.get_worker(f"pool:task{i}")
        assert worker is not None
        assert worker.status == "stopped"

    # The crash was logged + dropped (no restart this slice).
    assert any("crashed" in record.getMessage() for record in records), records
