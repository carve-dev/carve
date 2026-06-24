"""The ``sql`` step executor — render a .sql file, run it, capture rows.

``SqlStepExecutor`` implements the Unit-1 :class:`StepExecutor` seam for a
``sql`` step. It opens the step's ``connection`` by name (via the injected
connection factory), reads the ``.sql`` file (path-confined under the project
root), renders the file body through the **sandboxed** Jinja environment, runs
it as a single transaction, and captures the first-N rows as ``outputs``.

Single-file, single-transaction by design (spec §"Step executor: sql"): one
connection, one execute/query. Multi-statement files use the destination's
batch mechanism — the ``sql`` step is thin operational glue, not a SQL engine.

Path confinement
----------------
The ``.sql`` file is read from ``paths.root / step.file`` and the resolved path
must stay under ``paths.root`` (symlinks followed), mirroring
:meth:`carve.core.runners.local_venv.LocalVenvRunner._resolve_script`. An
escaping path is a clean ``failed`` :class:`StepResult`, never a read outside
the tree.

File-body Jinja
---------------
The file text is rendered through the sandboxed environment against the same
``{steps, run, env}`` namespace the launch-time ``jinja_vars`` use (see
:func:`carve.runtime.jinja_context.make_jinja_context`), plus a ``vars`` key
carrying the step's already-resolved ``jinja_vars`` (the cross-step output
threading happens upstream in ``_launch_step``, so a
``loaded_rows = "{{ steps.ingest.outputs.rows }}"`` arrives here as a concrete
value). A file body may therefore reference ``{{ steps.<id>.outputs.X }}`` or
``{{ env.X }}`` directly as well as ``{{ vars.<name> }}``. ``StrictUndefined``
makes a reference to a missing var a render error rather than a silent blank.

Multi-statement limitation
--------------------------
A multi-statement ``.sql`` file is classified by its *most-privileged*
statement (``classify`` takes the ``max``), so a file whose last statement is a
``SELECT`` but which also contains a write runs down the write path and does
**not** capture the trailing rows — a documented limitation of this thin
operational glue (the deeper fix is the destination's batch mechanism, not the
``sql`` step). A pure read file captures its rows as expected.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from jinja2.exceptions import TemplateError

from carve.core.sql.classify import SqlClassificationError, classify
from carve.runtime.jinja_context import build_sandbox, make_jinja_context
from carve.runtime.step_executor import StepResult
from carve.runtime.step_types.connections import (
    Connection,
    ConnectionResolutionError,
    ResolvedConnection,
    resolve_connection,
)

if TYPE_CHECKING:
    from pathlib import Path

    from carve.core.config.paths import ProjectPaths
    from carve.core.config.pipeline_schema import PipelineStep, SqlStepConfig
    from carve.core.config.schema import ConnectionsConfig
    from carve.runtime.run_context import PipelineRun
    from carve.runtime.step_types.connections import ConnectionFactory

# Default per-step wall-clock budget for a sql statement (spec: 5 min).
DEFAULT_SQL_TIMEOUT_SECONDS = 300

# First-N rows captured as outputs (over-fetch one to detect truncation).
DEFAULT_ROW_CAP = 100


class SqlStepExecutor:
    """Run a ``sql`` step: open the connection, render the file, capture rows."""

    step_type = "sql"

    def __init__(
        self,
        *,
        connections: ConnectionsConfig,
        connection_factory: ConnectionFactory | None = None,
        row_cap: int = DEFAULT_ROW_CAP,
        timeout_seconds: int = DEFAULT_SQL_TIMEOUT_SECONDS,
    ) -> None:
        """Build the executor.

        Args:
            connections: The ``[connections.*]`` config the factory looks names
                up in.
            connection_factory: The injected name → connector resolver
                (defaults to :func:`resolve_connection`). DuckDB-default keeps
                the sql path creds-free in tests.
            row_cap: Max rows captured into ``outputs`` (over-fetch one more to
                set the ``truncated`` flag).
            timeout_seconds: Per-step wall-clock budget for the statement.
        """
        self._connections = connections
        self._connection_factory = connection_factory or resolve_connection
        self._row_cap = row_cap
        self._timeout_seconds = timeout_seconds

    async def execute(
        self,
        *,
        step: PipelineStep,
        run: PipelineRun,
        paths: ProjectPaths,
    ) -> StepResult:
        """Resolve, render, run, and capture one ``sql`` step."""
        sql_step = _as_sql_step(step)

        try:
            resolved = self._connection_factory(sql_step.connection, self._connections)
        except ConnectionResolutionError as exc:
            return StepResult(status="failed", error_message=str(exc))

        try:
            sql_path = _confine_sql_file(paths.root, sql_step.file)
        except ValueError as exc:
            return StepResult(status="failed", error_message=str(exc))
        if not sql_path.is_file():
            return StepResult(
                status="failed",
                error_message=f"sql file not found: {sql_step.file}",
            )

        try:
            raw_sql = sql_path.read_text(encoding="utf-8")
        except OSError as exc:
            return StepResult(status="failed", error_message=f"failed to read sql file: {exc}")

        try:
            rendered_sql = _render_sql_body(raw_sql, step=sql_step, run=run)
        except TemplateError as exc:
            # The render happens BEFORE the worker thread, so the connection was
            # never opened (the session is still lazy). Close it here directly —
            # a no-op on the unopened session — rather than handing it to the
            # worker that now never runs.
            resolved.close()
            return StepResult(
                status="failed",
                error_message=f"failed to render sql file {sql_step.file!r}: {exc}",
            )

        # FIX-S2 (residual): the connection's open + close lifecycle lives
        # *inside* the worker thread, not in an ``async with resolved`` here.
        # Why: both shipped connectors open the real session **lazily** on the
        # first query — which runs inside the worker thread. ``wait_for`` can't
        # cancel a ``to_thread`` worker, so a ``with resolved:`` on this (main)
        # thread would run ``close()`` on timeout while the session is still
        # ``None`` (a no-op), then the orphaned worker would open the session
        # *after* close already ran → a leaked warehouse session per timed-out
        # step. By owning the ``with resolved:`` inside the worker (see
        # :func:`_open_run_close`), the worker itself closes the connection in
        # its own ``finally`` when it eventually finishes — even when this
        # coroutine has already abandoned the await and returned ``failed``.
        #
        # The deeper limitation that remains: the runaway query keeps running
        # until it completes (Python threads aren't interruptible) — a
        # driver-side query timeout is the Increment-4 fix — but the connection
        # is now LEAK-FREE: closed when the orphaned worker finishes.
        started = time.monotonic()
        try:
            # Offload the blocking DB work (open + run + close) to a thread —
            # the DAG walk is async — and bound it by the per-step wall-clock
            # budget. The worker can't be force-cancelled (no driver-side cancel
            # until live wiring, Increment 4), but ``wait_for`` returns control +
            # marks the step failed instead of blocking the walk; the abandoned
            # worker still runs to completion and closes the connection itself.
            outputs = await asyncio.wait_for(
                asyncio.to_thread(
                    _open_run_close,
                    resolved,
                    rendered_sql,
                    self._row_cap,
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            duration_ms = int((time.monotonic() - started) * 1000)
            return StepResult(
                status="failed",
                error_message=f"sql step timed out after {self._timeout_seconds}s",
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return StepResult(
                status="failed",
                error_message=f"sql execution failed: {exc}",
                duration_ms=duration_ms,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        return StepResult(
            status="succeeded",
            outputs=outputs,
            duration_ms=duration_ms,
        )


def _as_sql_step(step: PipelineStep) -> SqlStepConfig:
    """Narrow ``step`` to a ``sql`` step (the registry guarantees the type)."""
    from carve.core.config.pipeline_schema import SqlStepConfig

    if not isinstance(step, SqlStepConfig):
        raise TypeError(f"SqlStepExecutor received a {step.type!r} step: {step.id!r}")
    return step


def _confine_sql_file(root: Path, file: str) -> Path:
    """Resolve ``file`` under ``root``, rejecting traversal (symlinks followed).

    Mirrors ``LocalVenvRunner._resolve_script``: both paths are resolved before
    comparison so a symlink inside the tree pointing out is also caught.
    """
    root_resolved = root.resolve()
    candidate = (root_resolved / file).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"sql file path {file!r} resolves outside the project root") from exc
    return candidate


def _render_sql_body(raw_sql: str, *, step: SqlStepConfig, run: PipelineRun) -> str:
    """Render the .sql file body through the sandboxed Jinja environment.

    The namespace is the launch-time ``{steps, run, env}`` (reused from
    :func:`carve.runtime.jinja_context.make_jinja_context`) plus a ``vars`` key
    carrying the step's already-resolved ``jinja_vars`` (cross-step outputs
    threaded upstream). A file body may therefore reference
    ``{{ steps.<id>.outputs.X }}`` / ``{{ env.X }}`` directly as well as
    ``{{ vars.<name> }}``. ``StrictUndefined`` (inherited from the shared
    sandbox) surfaces a missing reference as a render error.

    ``steps`` is empty here because the upstream results aren't threaded into
    the executor — the cross-step values a body needs are already resolved into
    ``vars`` at launch time; the ``steps``/``env`` keys exist so a body written
    against the documented namespace renders without an ``Undefined`` error.
    """
    context = make_jinja_context(run=run, step_results={})
    context["vars"] = dict(step.jinja_vars)
    template = build_sandbox().from_string(raw_sql)
    return template.render(context)


def _jsonable(value: object) -> object:
    """Coerce a driver row value into a JSON-serializable primitive.

    The runtime persists step ``outputs`` as JSONB, so raw driver types must be
    reduced to JSON primitives before they enter ``outputs``:

    * ``date`` / ``datetime`` / ``time`` → ISO-8601 string,
    * ``Decimal`` → ``str`` (exact — a Decimal round-trips losslessly as text,
      where ``float`` would silently lose precision),
    * ``bytes`` → UTF-8 string when valid, else hex,
    * everything else (``str``/``int``/``float``/``bool``/``None``/nested
      ``list``/``dict``) passes through, recursing into containers.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    # Fall through: an unmapped exotic type — stringify so JSONB persistence
    # never fails (a typed driver value is better stringified than dropped).
    return str(value)


def _jsonable_row(row: dict[str, Any]) -> dict[str, object]:
    """Coerce every value in one row dict to a JSON primitive."""
    return {key: _jsonable(value) for key, value in row.items()}


def _open_run_close(resolved: ResolvedConnection, sql: str, row_cap: int) -> dict[str, Any]:
    """Open, run, and close the connection — all inside the worker thread.

    This runs on the ``asyncio.to_thread`` worker. The ``with resolved:`` owns
    the connector's full lifecycle here so the **same thread** that lazily opens
    the real session (on the first query) is the one that closes it — even when
    the awaiting coroutine has already timed out and abandoned this worker. That
    is the leak fix: a main-thread ``with resolved:`` would ``close()`` the
    still-``None`` session before the orphaned worker opened it (FIX-S2
    residual).
    """
    with resolved:
        return _execute_sql(resolved.connection, resolved.dialect, sql, row_cap)


def _execute_sql(connection: Connection, dialect: str, sql: str, row_cap: int) -> dict[str, Any]:
    """Run ``sql`` as a single transaction and capture the first-N rows.

    Reads (``SELECT``/``WITH``/``SHOW``/``DESCRIBE``) capture rows; everything
    else (DML/DDL) runs via ``execute`` and reports zero rows. The read/write
    split rides the shipped :func:`carve.core.sql.classify.classify`; an
    unclassifiable statement is run as a read attempt (the driver raises if it
    isn't), so the executor never silently swallows a write.

    Rows are over-fetched by one (``run_query`` with ``limit=row_cap + 1``) so
    truncation is detected without a second query — the driver caps the read
    rather than fetching every row then slicing in Python (the
    ``make_sql_tool`` pattern). Every captured value is coerced to a JSON
    primitive (:func:`_jsonable`) so ``outputs`` is JSONB-safe.

    ``outputs`` = ``{rows, row_count, truncated}``.
    """
    is_read = _is_read(sql, dialect)
    if not is_read:
        connection.execute(sql)
        return {"rows": [], "row_count": 0, "truncated": False}

    fetched = connection.run_query(sql, limit=row_cap + 1)
    truncated = len(fetched) > row_cap
    rows = [_jsonable_row(row) for row in fetched[:row_cap]]
    return {"rows": rows, "row_count": len(rows), "truncated": truncated}


def _is_read(sql: str, dialect: str) -> bool:
    """Classify ``sql`` as a read (vs write/DDL) via the shipped classifier.

    Falls back to "read" on an unclassifiable statement so the query path runs
    it and the driver decides — never silently treats an unknown statement as a
    write.
    """
    try:
        kind = classify(sql, dialect)
    except SqlClassificationError:
        return True
    return kind.is_read


__all__ = [
    "DEFAULT_ROW_CAP",
    "DEFAULT_SQL_TIMEOUT_SECONDS",
    "SqlStepExecutor",
]
