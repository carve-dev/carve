"""The reaper loop — deterministic under a FakeClock, fails runs, stops cleanly.

No Postgres: in-memory fakes for the ``JobQueue.reclaim_stale``/``_emit`` and the
``Repository.update_run_status`` seams. ``reap_stale_once`` is a pure single pass;
``reaper_loop`` is the async boundary loop. Covers:

* a stale set is reclaimed in one ``reap_stale_once`` pass, each reclaimed run is
  failed with ``worker_crashed_or_unreachable``, and ``job.reclaimed`` is emitted;
* a reclaimed job with a NULL ``run_id`` skips the run-fail without erroring;
* ``reaper_loop`` co-runs under a ``FakeClock`` and stops promptly on shutdown
  (bounded by a timeout — no spinning, no hang).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from carve.runtime.clock import FakeClock
from carve.runtime.reaper import reap_stale_once, reaper_loop


class _FakeQueue:
    """Returns a pre-seeded reclaim set once, then nothing; spies ``_emit``."""

    def __init__(self, reclaim: list[tuple[str, str | None, str | None]]) -> None:
        self._reclaim = reclaim
        self.reclaim_calls = 0
        self.emitted: list[tuple[str, dict[str, object]]] = []

    def reclaim_stale(
        self, now: datetime, *, stale_threshold_s: float = 60.0, tenant_id: int = 1
    ) -> list[tuple[str, str | None, str | None]]:
        self.reclaim_calls += 1
        # Only the first pass has stale jobs; later passes are empty (so a loop
        # doesn't re-fail the same runs forever).
        if self.reclaim_calls == 1:
            return self._reclaim
        return []

    def _emit(self, kind: str, payload: dict[str, object]) -> None:
        self.emitted.append((kind, payload))


class _FakeRepo:
    """Records ``update_run_status`` calls."""

    def __init__(self) -> None:
        self.status_calls: list[tuple[str, str, str | None]] = []

    def update_run_status(self, run_id: str, status: str, error: str | None = None) -> None:
        self.status_calls.append((run_id, status, error))


def test_reap_stale_once_reclaims_fails_runs_and_emits() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    queue = _FakeQueue([("job_a", "run_a", "worker-1"), ("job_b", "run_b", "worker-2")])
    repo = _FakeRepo()

    reclaimed = reap_stale_once(queue, repo, now)  # type: ignore[arg-type]

    assert reclaimed == ["job_a", "job_b"]
    # Both in-flight runs failed with the reclaim reason.
    assert repo.status_calls == [
        ("run_a", "failed", "worker_crashed_or_unreachable"),
        ("run_b", "failed", "worker_crashed_or_unreachable"),
    ]
    # job.reclaimed emitted per job.
    reclaimed_events = [e for e in queue.emitted if e[0] == "job.reclaimed"]
    assert [e[1]["job_id"] for e in reclaimed_events] == ["job_a", "job_b"]


def test_reap_stale_once_skips_run_fail_for_null_run_id() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    queue = _FakeQueue([("job_norun", None, "worker-1")])
    repo = _FakeRepo()

    reclaimed = reap_stale_once(queue, repo, now)  # type: ignore[arg-type]

    assert reclaimed == ["job_norun"]
    # No run to fail — the repo was never asked.
    assert repo.status_calls == []
    # Still emitted (with run_id None).
    reclaimed_events = [e for e in queue.emitted if e[0] == "job.reclaimed"]
    assert len(reclaimed_events) == 1
    assert reclaimed_events[0][1]["run_id"] is None


async def test_reaper_loop_runs_a_pass_then_stops_on_shutdown() -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    queue = _FakeQueue([("job_a", "run_a", "worker-1")])
    repo = _FakeRepo()
    shutdown = asyncio.Event()

    async def stop_after_first_reclaim() -> None:
        for _ in range(500):
            if queue.reclaim_calls >= 1 and repo.status_calls:
                shutdown.set()
                return
            await asyncio.sleep(0)
        shutdown.set()

    await asyncio.wait_for(
        asyncio.gather(
            reaper_loop(queue, repo, interval_s=30.0, clock=clock, shutdown=shutdown),  # type: ignore[arg-type]
            stop_after_first_reclaim(),
        ),
        timeout=5.0,
    )

    assert queue.reclaim_calls >= 1
    assert repo.status_calls == [("run_a", "failed", "worker_crashed_or_unreachable")]


async def test_reaper_loop_stops_immediately_when_preset_shutdown() -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    queue = _FakeQueue([])
    repo = _FakeRepo()
    shutdown = asyncio.Event()
    shutdown.set()  # pre-set: the loop exits after at most one pass

    await asyncio.wait_for(
        reaper_loop(queue, repo, interval_s=30.0, clock=clock, shutdown=shutdown),  # type: ignore[arg-type]
        timeout=2.0,
    )
