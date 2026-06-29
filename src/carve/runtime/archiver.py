"""The archiver loop — move terminal, aged-out runtime rows into ``*_archive`` tables.

The runtime's live tables (``jobs``/``runs``/``logs``/``step_runs``) grow without
bound as pipelines run. The archiver keeps them — and their hot indexes — small by
periodically moving rows that are both **terminal** and **older than the table's
retention window** into the matching ``*_archive`` table (created by migration
0011 as a ``LIKE … INCLUDING ALL EXCLUDING INDEXES`` clone, so an
``INSERT … SELECT *`` round-trips column-for-column and the archive carries no
FKs of its own).

No data loss — the load-bearing invariant
------------------------------------------
:func:`archive_table_safely` runs each table-batch in **ONE transaction**:
``INSERT … SELECT`` → verify the inserted rowcount equals the rows about to be
deleted → ``DELETE`` → ``commit``. The DELETE is **never** issued before the
verify, and *any* failure (a verification mismatch, an injected error, an FK/IO
error) propagates out of the ``with`` block, which closes the session and rolls
the whole batch back — so the active table is never short a row. Under the single
transaction an injected failure between INSERT and DELETE rolls the INSERT back
too; the property that matters is *no data loss / active table intact*.

Deterministic, like the scheduler/reaper
-----------------------------------------
The module splits a **synchronous, deterministic single pass**
(:func:`archive_once`, driven sleep-free under a ``FakeClock`` + an injected
``now`` in tests) from the **async boundary loop** (:func:`archiver_loop`) that
``carve serve`` hosts as a third co-running task. ``now`` is injected and the
per-table ``cutoff = now - window`` is computed in Python and passed as a bound
param — never SQL ``now()`` — so insert/verify/delete see one identical row set
and the window/terminal-status filtering is deterministic (the ``reclaim_stale``
discipline). The sync state-store calls are bridged off the event loop via
``asyncio.to_thread`` exactly as ``reaper.py`` does.

FK-safe ordering
----------------
``jobs``/``step_runs``/``logs`` all FK → ``runs.id``; a ``runs`` DELETE is blocked
while a child still references it. :data:`_ARCHIVE_TABLES` therefore orders the
pass children-first (``runs`` last), and the default windows stagger so a parent
run's children have already aged out by the time the run itself does.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import CursorResult

from carve.core.config.schema import ArchiveConfig, parse_duration
from carve.runtime.clock import Clock, system_clock

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session, sessionmaker

    from carve.runtime.events import EventSink

logger = logging.getLogger(__name__)

DEFAULT_ARCHIVE_INTERVAL_S = 3600.0


class ArchiveVerificationFailed(RuntimeError):
    """The archive INSERT didn't capture every row the batch was about to delete.

    Raised by :func:`archive_table_safely` *before* the ``DELETE`` when the
    ``INSERT … SELECT`` rowcount (or the subsequent delete count) disagrees with
    the active-table count taken at the top of the same transaction. Raising it
    rolls the batch back, so no active row is ever deleted without a verified
    archive copy — the no-data-loss invariant.
    """

    def __init__(
        self, *, table: str, expected: int, inserted: int, deleted: int | None = None
    ) -> None:
        self.table = table
        self.expected = expected
        self.inserted = inserted
        self.deleted = deleted
        detail = f"expected {expected}, archived {inserted}"
        if deleted is not None:
            detail += f", deleted {deleted}"
        super().__init__(f"archive verification failed for {table!r}: {detail}")


@dataclass(frozen=True)
class _TableSpec:
    """One table's archive predicate: its age column + terminal-status filter.

    ``status_filter`` is ``None`` for an age-only table (``logs`` has no status).
    ``tenant_scoped`` is ``True`` only for tables that carry a ``tenant_id``
    column (of these four, only ``jobs`` — the M1 ``runs``/``logs`` and
    ``step_runs`` predate multi-tenancy and have none, so they archive globally).
    ``window_attr`` names the :class:`ArchiveConfig` field carrying the retention
    window for this table.
    """

    table: str
    finished_col: str
    status_filter: tuple[str, ...] | None
    window_attr: str
    tenant_scoped: bool


# Children (jobs / step_runs / logs all FK -> runs.id) BEFORE the parent ``runs``,
# so a ``runs`` DELETE never trips a child FK within a pass. The per-table age
# column + terminal-status set are load-bearing — they do NOT apply uniformly
# (``runs`` ages on ``completed_at``, ``logs`` has no status at all, and only
# ``jobs`` carries a ``tenant_id`` column).
_ARCHIVE_TABLES: tuple[_TableSpec, ...] = (
    _TableSpec(
        table="jobs",
        finished_col="finished_at",
        status_filter=("succeeded", "failed", "cancelled", "timed_out"),
        window_attr="jobs_window",
        tenant_scoped=True,
    ),
    _TableSpec(
        table="step_runs",
        finished_col="finished_at",
        status_filter=("succeeded", "failed", "skipped"),
        window_attr="steps_window",
        tenant_scoped=False,
    ),
    _TableSpec(
        table="logs",
        finished_col="timestamp",
        status_filter=None,
        window_attr="logs_window",
        tenant_scoped=False,
    ),
    _TableSpec(
        table="runs",
        finished_col="completed_at",
        # ``runs`` uses the M1 run vocabulary — terminal status is "success",
        # NOT "succeeded" (the worker maps the result via _RUN_STATUS_BY_RESULT,
        # and a crash terminates a run as "crashed"). Do NOT "correct" this to
        # "succeeded": jobs/step_runs use "succeeded", but runs.status never does
        # (see repository.update_run_status's terminal set), so that would leave
        # every successful run un-archived.
        status_filter=("success", "failed", "cancelled", "crashed"),
        window_attr="runs_window",
        tenant_scoped=False,
    ),
)


def _build_predicate(
    *,
    finished_col: str,
    status_filter: tuple[str, ...] | None,
    cutoff: datetime,
    tenant_id: int,
    tenant_scoped: bool,
) -> tuple[str, dict[str, Any]]:
    """Build the ``WHERE`` clause + bound params shared by count/insert/delete.

    The table and column identifiers come from the fixed :data:`_ARCHIVE_TABLES`
    map (never user input), so they are safe to interpolate; every *value*
    (``cutoff``, ``tenant_id``, each terminal status) is a bound param. The
    ``tenant_id`` clause is added only for ``tenant_scoped`` tables — the M1
    ``runs``/``logs`` and ``step_runs`` have no such column.
    """
    clauses = [f"{finished_col} < :cutoff"]
    params: dict[str, Any] = {"cutoff": cutoff}
    if tenant_scoped:
        clauses.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if status_filter:
        placeholders = ", ".join(f":status_{i}" for i in range(len(status_filter)))
        clauses.append(f"status IN ({placeholders})")
        for i, status in enumerate(status_filter):
            params[f"status_{i}"] = status
    return " AND ".join(clauses), params


def _archive_insert(session: Session, table: str, predicate: str, params: dict[str, Any]) -> int:
    """``INSERT INTO <table>_archive SELECT * FROM <table> WHERE <predicate>``; return rowcount.

    Column-list-agnostic ``SELECT *`` is safe because the archive table is a
    ``LIKE INCLUDING ALL`` clone (identical column shape). Defined at module level
    so the verify-then-delete tests can patch the insert/delete seams.
    """
    # ``Session.execute`` is typed ``Result``; a DML statement always yields a
    # ``CursorResult`` (the only result type carrying ``rowcount``).
    result = session.execute(
        sa.text(f"INSERT INTO {table}_archive SELECT * FROM {table} WHERE {predicate}"),
        params,
    )
    assert isinstance(result, CursorResult)
    return result.rowcount


def _archive_delete(session: Session, table: str, predicate: str, params: dict[str, Any]) -> int:
    """``DELETE FROM <table> WHERE <predicate>``; return rowcount."""
    result = session.execute(
        sa.text(f"DELETE FROM {table} WHERE {predicate}"),
        params,
    )
    assert isinstance(result, CursorResult)
    return result.rowcount


def archive_table_safely(
    session_factory: sessionmaker[Session],
    table: str,
    *,
    cutoff: datetime,
    status_filter: tuple[str, ...] | None,
    finished_col: str,
    tenant_id: int = 1,
    tenant_scoped: bool = True,
) -> int:
    """Transactionally move ``table``'s aged-out terminal rows into ``<table>_archive``.

    ONE ``with session_factory() as session:`` transaction guarantees no data
    loss: count the active rows matching the batch predicate, ``INSERT … SELECT``
    them into the archive, **verify the inserted rowcount equals that count**, and
    only then ``DELETE`` and ``commit``. A verification mismatch raises
    :class:`ArchiveVerificationFailed` *before* the DELETE; any exception (that, an
    injected failure, an FK/IO error) closes the session and rolls the whole batch
    back, so the active table is never short a row. Returns the number of rows
    moved (``0`` when nothing matched — no INSERT/DELETE is issued).

    ``cutoff`` is a Python-computed bound param (``now - window``), so the count,
    insert, and delete all see one identical row set. ``tenant_scoped`` adds the
    ``tenant_id`` predicate only for tables that carry the column (``jobs``).
    """
    predicate, params = _build_predicate(
        finished_col=finished_col,
        status_filter=status_filter,
        cutoff=cutoff,
        tenant_id=tenant_id,
        tenant_scoped=tenant_scoped,
    )
    with session_factory() as session:
        expected = session.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE {predicate}"), params
        ).scalar_one()
        if expected == 0:
            return 0
        inserted = _archive_insert(session, table, predicate, params)
        # Verify BEFORE deleting: the archive must hold every row we are about to
        # remove. A mismatch halts the batch atomically (rollback on the way out).
        if inserted != expected:
            raise ArchiveVerificationFailed(table=table, expected=expected, inserted=inserted)
        deleted = _archive_delete(session, table, predicate, params)
        if deleted != expected:
            raise ArchiveVerificationFailed(
                table=table, expected=expected, inserted=inserted, deleted=deleted
            )
        session.commit()
        return inserted


def archive_once(
    session_factory: sessionmaker[Session],
    now: datetime,
    config: ArchiveConfig,
    *,
    emitter: EventSink | None = None,
    tenant_id: int = 1,
) -> dict[str, int]:
    """One deterministic sync pass: archive each table's aged-out terminal rows.

    Iterates :data:`_ARCHIVE_TABLES` children-first (``runs`` last, FK-safe).
    Per table computes ``cutoff = now - window`` (Python-side bound param),
    calls :func:`archive_table_safely`, and — for each table that succeeds — emits
    ``archive.batch_completed`` (payload ``{"table", "rows_moved"}``) through the
    injected ``emitter`` (a no-op when ``None``). A per-table failure is logged and
    the pass continues to the next table (the scheduler's per-row guard), so one
    bad table never aborts the others. Returns ``{table: rows_moved}`` for the
    tables that completed.
    """
    moved: dict[str, int] = {}
    for spec in _ARCHIVE_TABLES:
        window = parse_duration(getattr(config, spec.window_attr))
        cutoff = now - window
        try:
            rows = archive_table_safely(
                session_factory,
                spec.table,
                cutoff=cutoff,
                status_filter=spec.status_filter,
                finished_col=spec.finished_col,
                tenant_id=tenant_id,
                tenant_scoped=spec.tenant_scoped,
            )
        except Exception:
            logger.exception("archive pass for %s failed; skipping to next table", spec.table)
            continue
        moved[spec.table] = rows
        _emit(emitter, "archive.batch_completed", {"table": spec.table, "rows_moved": rows})
    total = sum(moved.values())
    if total:
        logger.info("archiver moved %d row(s) across %d table(s)", total, len(moved))
    return moved


async def archiver_loop(
    session_factory: sessionmaker[Session],
    config: ArchiveConfig,
    *,
    interval_s: float = DEFAULT_ARCHIVE_INTERVAL_S,
    clock: Clock = system_clock,
    emitter: EventSink | None = None,
    shutdown: asyncio.Event | None = None,
    tenant_id: int = 1,
) -> None:
    """Poll ``archive_once`` to the next wall-clock boundary until ``shutdown``.

    The async entry point ``carve serve`` runs alongside the scheduler + reaper.
    Each iteration bridges the sync ``archive_once`` off the event loop via
    ``asyncio.to_thread`` (passing ``clock.now()`` so the windows are evaluated
    against a single instant), then sleeps to the next ``interval_s`` boundary via
    ``clock``. A pass that raises is logged and swallowed so one bad poll never
    kills the loop — it backs off via the boundary sleep. ``shutdown`` (an
    ``asyncio.Event``) breaks the loop between sleeps for a clean stop. Mirrors
    ``reaper_loop``.
    """
    shutdown = shutdown or asyncio.Event()
    while not shutdown.is_set():
        now = clock.now()
        try:
            await asyncio.to_thread(
                archive_once,
                session_factory,
                now,
                config,
                emitter=emitter,
                tenant_id=tenant_id,
            )
        except Exception:
            logger.exception("archiver pass failed; backing off to next boundary")
        if shutdown.is_set():
            break
        # Race the boundary sleep against shutdown so Ctrl-C/SIGTERM doesn't wait
        # out the full interval.
        sleeper = asyncio.create_task(clock.sleep_until_next_boundary(interval_s))
        waiter = asyncio.create_task(shutdown.wait())
        try:
            await asyncio.wait(
                {sleeper, waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (sleeper, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, waiter, return_exceptions=True)


def _emit(emitter: EventSink | None, kind: str, payload: dict[str, Any]) -> None:
    """Emit ``kind``/``payload`` through ``emitter`` when one is injected; else no-op.

    Mirrors ``JobQueue._emit``: with no emitter (the back-compat default) it does
    nothing; with one it delegates to ``emitter.emit`` (which swallows its own
    failures, so a down events table never aborts an archive pass).
    """
    if emitter is not None:
        emitter.emit(kind, payload)


__all__ = [
    "DEFAULT_ARCHIVE_INTERVAL_S",
    "ArchiveVerificationFailed",
    "archive_once",
    "archive_table_safely",
    "archiver_loop",
]
