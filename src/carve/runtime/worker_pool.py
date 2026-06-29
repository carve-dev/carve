"""The in-process worker pool — fan ``carve serve``/``carve worker`` out to N workers.

One coroutine, :func:`run_worker_pool`, runs N :func:`~carve.runtime.worker.worker_loop`
tasks over the **one** shared ``JobQueue``/state store (the queue is the only
coordination point — there are no per-worker DB handles). Each task gets a unique
``:taskN``-suffixed ``worker_id`` carved off the base id, so the ``workers`` rows,
claims, and heartbeats stay distinct while the session pool is shared.

The delta this slice adds over what already shipped is small and precise — the
queue, the ``expected_worker_id`` ownership guard (so the pool inherits
zombie-no-stomp for free), the reaper, the heartbeat loop, and ``worker_loop``'s
own drain handling are all done. ``worker_loop`` already stops claiming the
instant its shared ``shutdown`` is set, finishes its in-flight ``run_once``
**un-cancelled**, and ``unregister_worker``s in its ``finally`` even on cancel.
So graceful drain is just **set the shared ``shutdown`` Event**; the only net-new
logic here is:

* **the grace timeout** — wait up to ``grace_period_s`` for the workers to drain,
  then cancel the stragglers (their ``finally`` still unregisters; the interrupted
  in-flight job is left ``running``/stale for the already-shipped reaper);
* **the second-signal skip** — a ``force`` Event (set by the 2nd SIGINT/SIGTERM)
  that cancels the workers immediately, skipping the grace wait.

**Crash isolation is the real design call.** The N tasks are joined under
:func:`asyncio.gather` with ``return_exceptions=True`` — **not** a bare
``TaskGroup``. A bare ``TaskGroup`` cancels every sibling the moment one child
raises; that is the correct shared-fate semantics for ``carve serve``'s 3 daemon
loops (each swallows its own per-pass errors, so they don't raise) but the **wrong**
semantics for the pool: a worker that escapes its own guard (e.g.
``register_worker``/``unregister_worker`` raising, outside ``run_once``'s catch)
must be logged and dropped without taking down its siblings — or ``serve``'s
scheduler/reaper/archiver. So ``serve`` adds the pool as a *single* TaskGroup
child (it shares the one shutdown Event), but the pool's internal join is the
isolating ``gather``. Restarting a crashed worker is out of scope this slice —
log + drop is the contract.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from carve.runtime.worker import (
    DEFAULT_POLL_INTERVAL_S,
    make_worker_id,
    worker_loop,
)

if TYPE_CHECKING:
    from carve.runtime.worker import WorkerContext

logger = logging.getLogger(__name__)

# The graceful-drain budget: once ``shutdown`` is set the pool waits this long for
# every worker to finish its in-flight job, then cancels the stragglers. 5 minutes
# matches the spec's "graceful shutdown completes within the configured grace
# period or escalates cleanly".
DEFAULT_GRACE_PERIOD_S = 300.0


async def run_worker_pool(
    ctx: WorkerContext,
    *,
    workers: int,
    shutdown: asyncio.Event,
    force: asyncio.Event | None = None,
    grace_period_s: float = DEFAULT_GRACE_PERIOD_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Run ``workers`` ``worker_loop`` tasks over one shared queue until drained.

    Builds ``workers`` contexts from ``ctx`` — each a copy with a unique
    ``<base>:task{i}`` ``worker_id`` (the base is ``ctx.worker_id`` or a fresh
    :func:`make_worker_id`) — reusing the single shared
    ``Repository``/``JobQueue``/``EventEmitter``/registry. Spawns one
    ``worker_loop`` per context, **all sharing the one ``shutdown`` Event** so a
    single set drains the whole pool.

    Lifecycle:

    1. **Serve** — return-less wait on ``shutdown``. The workers claim + run jobs;
       a worker that crashes (escapes its guard) is collected and logged in the
       ``finally`` while its siblings keep going (that is the ``gather`` isolation).
    2. **Drain** — once ``shutdown`` is set every ``worker_loop`` has already
       stopped claiming and is finishing its in-flight ``run_once``. Wait up to
       ``grace_period_s`` for them all to drain, cut short the instant ``force``
       (the 2nd signal) fires.
    3. **Escalate** — on grace-expiry or ``force``, cancel any straggler; their
       ``finally`` still ``unregister_worker``s and the interrupted in-flight job
       is left ``running``/stale for the reaper. The pool always ends by joining
       every task under ``gather(return_exceptions=True)`` so a crashed worker is
       logged + dropped, never re-raised into ``serve``.
    """
    if workers < 1:
        return
    force = force or asyncio.Event()
    base = ctx.worker_id or make_worker_id()
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(
            worker_loop(
                replace(ctx, worker_id=f"{base}:task{i}"),
                poll_interval_s=poll_interval_s,
                shutdown=shutdown,
            ),
            name=f"{base}:task{i}",
        )
        for i in range(workers)
    ]
    try:
        # Phase 1 — serve the queue until a drain is requested. ``worker_loop``
        # runs until ``shutdown`` is set; a crashed worker does NOT end this wait
        # (its siblings keep running) — it is surfaced when we join below.
        await shutdown.wait()
        # Phase 2 — graceful drain, bounded by the grace period or a 2nd signal.
        await _drain(tasks, force, grace_period_s)
    finally:
        # Phase 3 — escalate + always join. Cancel any straggler still in-flight
        # (grace expired or ``force`` fired); ``worker_loop``'s ``finally`` still
        # unregisters and leaves the interrupted job stale for the reaper. The
        # gather collects the cancellations AND any crash so neither can ever
        # propagate out of the pool into ``serve``.
        for task in tasks:
            if not task.done():
                task.cancel()
        results: list[Any] = await asyncio.gather(*tasks, return_exceptions=True)
        _log_worker_crashes(tasks, results)


async def _drain(
    tasks: list[asyncio.Task[None]],
    force: asyncio.Event,
    grace_period_s: float,
) -> None:
    """Wait until every task drains, ``force`` fires, or ``grace_period_s`` elapses.

    Leaves the worker tasks running (it does not consume their results — the pool
    joins them in its ``finally``). Mirrors ``reaper_loop``'s race-two-then-cancel
    pattern: ``_all_done`` (a wrapper over ``asyncio.wait(tasks)``, which never
    cancels its inputs) raced against ``force``, both torn down on exit.
    """
    all_done = asyncio.ensure_future(_all_done(tasks))
    forced = asyncio.ensure_future(force.wait())
    try:
        await asyncio.wait(
            {all_done, forced},
            timeout=grace_period_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for fut in (all_done, forced):
            if not fut.done():
                fut.cancel()
        # Tear down the two race wrappers only — never the worker tasks (the pool's
        # ``finally`` owns the worker cancellation, the single escalation path).
        await asyncio.gather(all_done, forced, return_exceptions=True)


async def _all_done(tasks: list[asyncio.Task[None]]) -> None:
    """Complete once every worker task is done. Cancelling this leaves them running.

    Wraps ``asyncio.wait`` (not ``gather``) deliberately: cancelling a ``gather``
    propagates to its children, but cancelling an ``asyncio.wait`` does not touch
    the awaited tasks — so the pool's single, explicit cancellation path stays the
    only one that ever stops a worker.
    """
    if tasks:
        await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)


def _log_worker_crashes(tasks: list[asyncio.Task[None]], results: list[Any]) -> None:
    """Log every worker that escaped its own guard; cancellations are not crashes.

    ``results`` is aligned with ``tasks`` (``gather`` preserves order). A
    ``CancelledError`` is the pool's own escalation, not a fault; any other
    exception is a worker that crashed past ``run_once``'s catch — logged and
    dropped (no restart this slice).
    """
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            continue
        if isinstance(result, BaseException):
            logger.error(
                "worker %s crashed and was dropped (no restart this slice): %r",
                task.get_name(),
                result,
            )


__all__ = [
    "DEFAULT_GRACE_PERIOD_S",
    "run_worker_pool",
]
