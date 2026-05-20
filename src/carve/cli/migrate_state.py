"""`carve migrate-state` — one-shot SQLite -> Postgres state-store migrator.

v0.1-01 retired SQLite as a runtime backend. Walking-skeleton users with
an existing ``.carve/state.db`` need a way to bring their history into a
Postgres database without losing rows. This command is that one-time
bridge.

Behavior (six steps, in order):

1. **Validate.** Connect to both sides. The SQLite source must be at
   one of Alembic revisions ``0001`` .. ``0006`` (i.e. a real M1-era
   database). The Postgres target must be empty across the five state
   tables — refuses to clobber existing rows unless ``--force`` is set.
   Refuses outright if any ``runs`` row in the source has
   ``status IN ('running', 'queued')`` — the user must wait for those
   runs to terminate before migrating, otherwise the copy would land
   inconsistent state.
2. **Upgrade Postgres.** Run ``alembic upgrade head`` against the
   target so the schema matches the current ORM models.
3. **Copy.** ``SELECT *`` from each source table in dependency-safe
   order (``pipelines`` -> ``plans`` -> ``builds`` -> ``runs`` ->
   ``logs``) and insert into the target in 1000-row batches. JSON
   columns are parsed from TEXT into ``dict`` so they land cleanly in
   JSONB. Naive timestamps from SQLite are attached UTC ``tzinfo`` on
   the way out, matching the Postgres ``TIMESTAMPTZ`` shape.
4. **Verify.** ``SELECT COUNT(*)`` on both sides per table. Mismatch
   on any table fails with non-zero exit.
5. **Report.** A per-table summary table with row counts and elapsed
   time, plus a backup recommendation for the source ``.db`` file.
6. **Non-destructive.** The source SQLite file is never modified.

The migrator is intentionally *not* importable as a library — it lives
in `cli/` alongside the other Typer commands and is invoked via
``carve migrate-state``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import (
    Engine,
    MetaData,
    create_engine,
    select,
    text,
)
from sqlalchemy import (
    Table as SATable,
)
from sqlalchemy.engine import Row

from carve.core.state.database import (
    StateStoreBackendError,
    create_sqlite_source_engine,
    initialize_database,
)

console = Console()
_logger = logging.getLogger(__name__)


# Five state tables in dependency-safe insertion order: parents first,
# children last. ``logs`` references ``runs``; ``runs`` references
# ``pipelines`` and itself (parent_run_id); ``builds`` references
# ``pipelines`` and ``plans``; ``plans`` references ``pipelines``;
# ``pipelines`` references ``runs.id`` (last_run_id) and ``builds.id``
# (current_build_id) — that pair of forward references is broken by
# inserting pipelines with NULL FKs first, then patching them after
# runs/builds land. The patch happens implicitly because we copy the
# stored values verbatim and Postgres accepts forward FKs within a
# single transaction.
_TABLE_ORDER: tuple[str, ...] = (
    "pipelines",
    "plans",
    "builds",
    "runs",
    "logs",
)

# Columns whose source value is a JSON-encoded TEXT on SQLite and a
# JSONB dict on Postgres. The migrator parses these on read so psycopg
# inserts them as native JSONB rather than a string-in-JSONB.
_JSON_COLUMNS: Mapping[str, frozenset[str]] = {
    "plans": frozenset({"task_graph_json"}),
    "builds": frozenset({"manifest_json"}),
}

_BATCH_SIZE = 1000


class MigrationError(RuntimeError):
    """Raised when a validation step fails or a verify step mismatches."""


@dataclass(frozen=True)
class _TableReport:
    """Per-table outcome surfaced in the final report."""

    name: str
    rows_copied: int
    elapsed_seconds: float


def command(
    from_url: str = typer.Option(
        ...,
        "--from",
        help="Source SQLite URL, e.g. sqlite:///.carve/state.db",
    ),
    to_url: str = typer.Option(
        ...,
        "--to",
        help="Target Postgres URL, e.g. postgresql+psycopg://user:pw@host:5432/carve",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Override the 'target already populated' guard. Existing "
            "rows in the target are left in place; the copy is best-effort "
            "and may fail on duplicate primary keys."
        ),
    ),
) -> None:
    """Copy M1-shape state from a SQLite database into Postgres."""
    started = time.perf_counter()
    try:
        reports = _run_migration(from_url=from_url, to_url=to_url, force=force)
    except MigrationError as exc:
        console.print(f"[red]migration failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except StateStoreBackendError as exc:
        # The runtime engine factory refused the --to URL because it
        # wasn't a Postgres URL. Surface the friendly message and exit.
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    total_elapsed = time.perf_counter() - started
    _render_summary(reports, total_elapsed=total_elapsed, target_url=_mask(to_url))
    raise typer.Exit(code=0)


def _run_migration(
    *,
    from_url: str,
    to_url: str,
    force: bool,
) -> list[_TableReport]:
    """Drive the six-step migration. Returns per-table reports on success."""
    source_engine = create_sqlite_source_engine(from_url)
    # The target URL must be Postgres. Build the engine here (not via
    # the config-driven factory) because the migrator takes the URL
    # straight from the CLI flag — there's no `Config` in scope.
    if not (
        to_url.startswith("postgresql://")
        or to_url.startswith("postgresql+psycopg://")
    ):
        raise MigrationError(
            f"--to must be a Postgres URL (postgresql+psycopg://...); "
            f"got {_mask(to_url)!r}"
        )
    target_engine = create_engine(to_url, echo=False, future=True, pool_pre_ping=True)

    try:
        _validate_source_revision(source_engine)
        _validate_no_inflight_runs(source_engine)
        # 2. Upgrade Postgres schema to head. Idempotent — a target that
        # is already at head is a fast no-op.
        initialize_database(target_engine)
        if not force:
            _validate_target_empty(target_engine)
        reports = _copy_all_tables(source_engine, target_engine)
        _verify_counts(source_engine, target_engine, reports)
        return reports
    finally:
        source_engine.dispose()
        target_engine.dispose()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_VALID_SOURCE_REVISIONS: frozenset[str] = frozenset(
    {
        "0001_baseline",
        "0002_pipeline_centric",
        "0003_rename_apply_to_deploy",
        "0004_build_entity",
        "0005_runs_target",
        "0006_recovery_chains",
    }
)


def _validate_source_revision(engine: Engine) -> None:
    """Confirm the SQLite source carries an expected M1 Alembic revision."""
    with engine.connect() as conn:
        # alembic_version may be missing entirely on a freshly-created
        # SQLite DB that bypassed migrations (very early M1 prototypes).
        # Treat that as an unmigrated source and refuse to proceed.
        result = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'alembic_version'"
            )
        ).first()
        if result is None:
            raise MigrationError(
                "source SQLite database has no alembic_version table — "
                "this isn't a Carve M1 state store, or it pre-dates the "
                "migration system. Aborting."
            )
        row = conn.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        ).first()
        if row is None:
            raise MigrationError(
                "source SQLite database has an empty alembic_version "
                "table. Expected one of 0001..0006."
            )
        version: str = row[0]
        if version not in _VALID_SOURCE_REVISIONS:
            raise MigrationError(
                f"source SQLite database is at alembic revision {version!r}; "
                f"expected one of {sorted(_VALID_SOURCE_REVISIONS)}. Aborting."
            )


def _validate_no_inflight_runs(engine: Engine) -> None:
    """Refuse to run if any ``runs`` row is queued or running on the source."""
    with engine.connect() as conn:
        # Existence-only query; we don't need the actual ids in the
        # error message. The runs table is the only place we care about.
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM runs "
                "WHERE status IN ('running', 'queued')"
            )
        ).first()
        count = int(row[0]) if row is not None else 0
        if count > 0:
            raise MigrationError(
                f"source has {count} run(s) in status running/queued. "
                "Wait for those runs to complete (or mark them as "
                "crashed/cancelled) before migrating — copying mid-flight "
                "runs would land inconsistent state."
            )


def _validate_target_empty(engine: Engine) -> None:
    """Refuse to overwrite a target that already has state rows (no --force)."""
    with engine.connect() as conn:
        for table in _TABLE_ORDER:
            row = conn.execute(
                text(f'SELECT COUNT(*) FROM "{table}"')
            ).first()
            count = int(row[0]) if row is not None else 0
            if count > 0:
                raise MigrationError(
                    f"target Postgres already has {count} row(s) in "
                    f"{table!r}. Pass --force to migrate anyway (existing "
                    "rows are kept; duplicate primary keys will fail the copy)."
                )


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


def _copy_all_tables(
    source_engine: Engine,
    target_engine: Engine,
) -> list[_TableReport]:
    """Copy each state table in dependency-safe order. Returns per-table reports."""
    # Reflect target schema once — gives us the authoritative column list
    # for INSERTs. We use the source's column shape as a fallback (some
    # M1 dbs may have legacy columns the target schema dropped, e.g.
    # plans.estimates_json), and only insert intersecting columns.
    target_metadata = MetaData()
    target_metadata.reflect(bind=target_engine, only=_TABLE_ORDER)
    # Reflect only the tables actually present in the source. Older
    # M1 revisions (0001, pre-pipelines / pre-builds) don't carry every
    # table — we skip those gracefully in the copy loop.
    source_metadata = MetaData()
    with source_engine.connect() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name IN "
                    "('pipelines', 'plans', 'builds', 'runs', 'logs')"
                )
            )
        }
    source_metadata.reflect(bind=source_engine, only=sorted(existing))

    reports: list[_TableReport] = []
    for table_name in _TABLE_ORDER:
        started = time.perf_counter()
        if table_name not in existing:
            reports.append(
                _TableReport(
                    name=table_name,
                    rows_copied=0,
                    elapsed_seconds=0.0,
                )
            )
            continue
        rows_copied = _copy_table(
            source_engine=source_engine,
            target_engine=target_engine,
            source_table=source_metadata.tables[table_name],
            target_table=target_metadata.tables[table_name],
        )
        elapsed = time.perf_counter() - started
        reports.append(
            _TableReport(
                name=table_name,
                rows_copied=rows_copied,
                elapsed_seconds=elapsed,
            )
        )
    return reports


def _copy_table(
    *,
    source_engine: Engine,
    target_engine: Engine,
    source_table: SATable,
    target_table: SATable,
) -> int:
    """Copy a single table from source to target. Returns row count copied.

    Each batch runs in its own transaction with
    ``session_replication_role = 'replica'`` so cross-table forward FKs
    don't block the copy order. The pipelines table references
    ``runs.id`` (last_run_id) and ``builds.id`` (current_build_id), and
    runs has a self-FK (parent_run_id) — none of those can be honored
    while inserting in dependency order, but they're all valid on the
    source so disabling enforcement during the copy is safe.
    """
    source_columns = {c.name for c in source_table.columns}
    target_columns = {c.name for c in target_table.columns}
    # Intersect — drop legacy columns the target schema no longer carries
    # (e.g. ``plans.estimates_json`` lost in migration 0004).
    columns = [c for c in target_table.columns if c.name in source_columns]
    column_names = [c.name for c in columns]
    missing_from_source = target_columns - source_columns
    if missing_from_source:
        _logger.info(
            "table %s: %d target column(s) absent on source (%s); "
            "letting target defaults fill them in",
            target_table.name,
            len(missing_from_source),
            sorted(missing_from_source),
        )

    json_columns = _JSON_COLUMNS.get(target_table.name, frozenset())
    rows_copied = 0

    with source_engine.connect() as source_conn:
        result = source_conn.execution_options(
            stream_results=True,
            yield_per=_BATCH_SIZE,
        ).execute(select(*[source_table.c[name] for name in column_names]))

        # SQLAlchemy 2.x's `partitions()` yields lists of `Row`s in
        # batch-sized chunks; on the last partition the chunk is short.
        for chunk in result.partitions(_BATCH_SIZE):
            payload = [
                _coerce_row_for_target(
                    row=row,
                    column_names=column_names,
                    json_columns=json_columns,
                )
                for row in chunk
            ]
            if not payload:
                continue
            with target_engine.begin() as target_conn:
                # `replica` bypasses FK and triggers for the current
                # transaction. We need this because the dependency
                # order (pipelines first) implies forward FKs into
                # runs / builds that don't yet exist.
                target_conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
                target_conn.execute(target_table.insert(), payload)
            rows_copied += len(payload)

    return rows_copied


def _coerce_row_for_target(
    *,
    row: Row[Any],
    column_names: list[str],
    json_columns: Iterable[str],
) -> dict[str, Any]:
    """Convert a SQLite row into a dict that Postgres can accept verbatim.

    Two coercions:

    1. JSON columns stored as TEXT on SQLite are parsed into ``dict``
       so psycopg writes them as native JSONB rather than a string.
    2. Naive timestamps round-tripped from SQLite get UTC ``tzinfo``
       attached so they fit into ``TIMESTAMPTZ`` columns.
    """
    json_set = frozenset(json_columns)
    out: dict[str, Any] = {}
    for name, value in zip(column_names, row, strict=True):
        if value is None:
            out[name] = None
            continue
        if name in json_set and isinstance(value, str):
            try:
                out[name] = json.loads(value)
            except (TypeError, ValueError):
                # Malformed JSON-in-TEXT — preserve as an empty object
                # rather than blocking the whole migration on one bad row.
                _logger.warning(
                    "row in column %r has malformed JSON; substituting {}",
                    name,
                )
                out[name] = {}
            continue
        if isinstance(value, datetime) and value.tzinfo is None:
            out[name] = value.replace(tzinfo=UTC)
            continue
        out[name] = value
    return out


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _verify_counts(
    source_engine: Engine,
    target_engine: Engine,
    reports: list[_TableReport],
) -> None:
    """``SELECT COUNT(*)`` on both sides per table; raise on mismatch.

    Reads the *current* counts from both sides — note that on a
    ``--force`` run the target may legitimately have more rows than the
    source (preserved pre-existing rows). The verify step in that case
    asserts that the target count is at least the source count, not
    strict equality.
    """
    with source_engine.connect() as source_conn, target_engine.connect() as target_conn:
        # Existence check up front so we don't try to count a table the
        # source schema never had (older M1 revisions lack pipelines /
        # builds).
        existing_tables = {
            row[0]
            for row in source_conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name IN "
                    "('pipelines', 'plans', 'builds', 'runs', 'logs')"
                )
            )
        }
        for report in reports:
            if report.name not in existing_tables:
                continue
            src_row = source_conn.execute(
                text(f'SELECT COUNT(*) FROM "{report.name}"')
            ).first()
            tgt_row = target_conn.execute(
                text(f'SELECT COUNT(*) FROM "{report.name}"')
            ).first()
            src_count = int(src_row[0]) if src_row is not None else 0
            tgt_count = int(tgt_row[0]) if tgt_row is not None else 0
            if tgt_count < src_count:
                raise MigrationError(
                    f"verify failed for table {report.name!r}: "
                    f"source={src_count}, target={tgt_count}. "
                    "The target is short — rerun with --force to retry, "
                    "or inspect the source for rows that violate target "
                    "constraints (FKs, NOT NULL, CHECK)."
                )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _render_summary(
    reports: list[_TableReport],
    *,
    total_elapsed: float,
    target_url: str,
) -> None:
    """Print a per-table summary plus the backup recommendation."""
    table = Table(title="carve migrate-state — migration summary")
    table.add_column("Table", style="cyan")
    table.add_column("Rows copied", justify="right")
    table.add_column("Elapsed (s)", justify="right")
    total_rows = 0
    for report in reports:
        table.add_row(
            report.name,
            f"{report.rows_copied:,}",
            f"{report.elapsed_seconds:.3f}",
        )
        total_rows += report.rows_copied
    table.add_row(
        "[bold]total[/bold]",
        f"[bold]{total_rows:,}[/bold]",
        f"[bold]{total_elapsed:.3f}[/bold]",
    )
    console.print(table)
    console.print(f"target: [green]{target_url}[/green]")
    console.print(
        "[yellow]reminder:[/yellow] the source SQLite file was not "
        "modified. Back it up to durable storage before discarding."
    )


def _mask(url: str) -> str:
    """Strip credentials out of a Postgres URL for display.

    Replaces ``user:password@host`` with ``user:***@host``. Returns the
    URL unchanged if no credentials are present.
    """
    if "@" not in url:
        return url
    scheme_sep = "://"
    if scheme_sep not in url:
        return url
    scheme, rest = url.split(scheme_sep, 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}{scheme_sep}{user}:***@{host}"
    return url


__all__ = ["MigrationError", "command"]
