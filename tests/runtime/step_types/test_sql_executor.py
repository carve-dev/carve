"""SqlStepExecutor: real DuckDB, file-body Jinja, output capture, path confinement.

DuckDB is creds-free + in-process, so the whole sql path runs offline (it
substitutes for the spec's "fixture Postgres"). A file-backed DuckDB database
shared by name across two steps proves the steps resolve against the *same*
store: DDL in one step is visible to a later step's SELECT — even though the
executor closes its connection at the end of each step (FIX-S2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import SqlStepConfig
from carve.core.config.schema import ConnectionsConfig, DuckDBConnection
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_types.connections import ConnectionResolutionError, ResolvedConnection
from carve.runtime.step_types.sql import SqlStepExecutor

duckdb = pytest.importorskip("duckdb")


@pytest.fixture
def paths(tmp_path: Path) -> ProjectPaths:
    (tmp_path / "sql").mkdir()
    (tmp_path / "pipelines").mkdir()
    return ProjectPaths.from_root(tmp_path)


@pytest.fixture
def connections() -> ConnectionsConfig:
    return ConnectionsConfig(duckdb={"local": DuckDBConnection(path=":memory:")})


def _write_sql(paths: ProjectPaths, name: str, body: str) -> None:
    (paths.root / "sql" / name).write_text(body, encoding="utf-8")


def _file_db_factory(db_path: Path) -> Any:
    """A factory resolving every name to a fresh connection on one file DB.

    The executor owns + closes the connection per step (FIX-S2), so a
    ``:memory:`` connection can't carry a table across steps. A file-backed DB
    persists: step 1's DDL is visible to step 2's SELECT because both resolve
    against the same file — proving the same-store semantics a real pipeline
    relies on, while letting the executor close each step's connection.
    """
    from carve.core.connectors.duckdb import DIALECT, DuckDBConnection

    def _factory(_name: str, _config: ConnectionsConfig) -> ResolvedConnection:
        return ResolvedConnection(DuckDBConnection(database=str(db_path)), DIALECT)

    return _factory


def _step(file: str = "sql/q.sql", **overrides: Any) -> SqlStepConfig:
    base: dict[str, Any] = {"id": "q", "file": file, "connection": "local"}
    base.update(overrides)
    return SqlStepConfig(**base)


# --- output capture --------------------------------------------------------


async def test_select_captures_rows(paths: ProjectPaths, connections: ConnectionsConfig) -> None:
    _write_sql(paths, "q.sql", "SELECT 1 AS a, 'x' AS b")
    executor = SqlStepExecutor(connections=connections)

    result = await executor.execute(step=_step(), run=PipelineRun(pipeline="p"), paths=paths)

    assert result.status == "succeeded"
    assert result.outputs["rows"] == [{"a": 1, "b": "x"}]
    assert result.outputs["row_count"] == 1
    assert result.outputs["truncated"] is False


async def test_row_cap_truncates(paths: ProjectPaths, connections: ConnectionsConfig) -> None:
    _write_sql(paths, "q.sql", "SELECT * FROM range(5) AS t(n)")
    executor = SqlStepExecutor(connections=connections, row_cap=2)

    result = await executor.execute(step=_step(), run=PipelineRun(pipeline="p"), paths=paths)

    assert result.status == "succeeded"
    assert result.outputs["row_count"] == 2
    assert result.outputs["truncated"] is True


async def test_exact_cap_is_not_truncated(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    _write_sql(paths, "q.sql", "SELECT * FROM range(2) AS t(n)")
    executor = SqlStepExecutor(connections=connections, row_cap=2)

    result = await executor.execute(step=_step(), run=PipelineRun(pipeline="p"), paths=paths)

    assert result.outputs["row_count"] == 2
    assert result.outputs["truncated"] is False


# --- same-store semantics across steps (per-step connection ownership) ------


async def test_ddl_then_select_see_same_store(paths: ProjectPaths, tmp_path: Path) -> None:
    db_path = tmp_path / "shared.duckdb"
    factory = _file_db_factory(db_path)
    connections = ConnectionsConfig(duckdb={"local": DuckDBConnection()})
    executor = SqlStepExecutor(connections=connections, connection_factory=factory)
    run = PipelineRun(pipeline="p")

    _write_sql(paths, "create.sql", "CREATE TABLE t AS SELECT 7 AS v")
    create_result = await executor.execute(step=_step(file="sql/create.sql"), run=run, paths=paths)
    assert create_result.status == "succeeded"
    # A write reports zero rows captured.
    assert create_result.outputs == {"rows": [], "row_count": 0, "truncated": False}

    # A fresh connection (step 2) sees step 1's DDL because both resolve against
    # the same file DB — even though step 1's connection was closed (FIX-S2).
    _write_sql(paths, "read.sql", "SELECT v FROM t")
    read_result = await executor.execute(step=_step(file="sql/read.sql"), run=run, paths=paths)
    assert read_result.status == "succeeded"
    assert read_result.outputs["rows"] == [{"v": 7}]


# --- file-body Jinja against the cross-step namespace ----------------------


async def test_file_body_renders_jinja_vars(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    # The step's jinja_vars are already resolved upstream (cross-step outputs);
    # the file body references them via {{ vars.<name> }}.
    _write_sql(paths, "q.sql", "SELECT {{ vars.loaded_rows }} AS n")
    step = _step(jinja_vars={"loaded_rows": "42"})

    result = await SqlStepExecutor(connections=connections).execute(
        step=step, run=PipelineRun(pipeline="p"), paths=paths
    )

    assert result.status == "succeeded"
    assert result.outputs["rows"] == [{"n": 42}]


async def test_file_body_renders_run_namespace(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    _write_sql(paths, "q.sql", "SELECT '{{ run.target }}' AS t")
    result = await SqlStepExecutor(connections=connections).execute(
        step=_step(), run=PipelineRun(pipeline="p", target="prod"), paths=paths
    )
    assert result.outputs["rows"] == [{"t": "prod"}]


async def test_file_body_can_reference_steps_and_env_namespace(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    # FIX-S5: the file body renders against the same {steps, run, env} namespace
    # the launch-time jinja_vars use — not just {vars, run}. The top-level
    # ``steps``/``env`` keys are in scope (here both empty, so a length/lookup
    # against them renders to 0/default), proving the wider namespace is present
    # rather than raising an Undefined error on the bare ``steps``/``env`` name.
    _write_sql(
        paths,
        "q.sql",
        "SELECT {{ steps | length }} AS n, '{{ env.get('NOPE', 'e') }}' AS e",
    )
    result = await SqlStepExecutor(connections=connections).execute(
        step=_step(), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "succeeded"
    assert result.outputs["rows"] == [{"n": 0, "e": "e"}]


async def test_undefined_jinja_var_is_failed(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    # StrictUndefined: a missing reference is a render error, not a silent blank.
    _write_sql(paths, "q.sql", "SELECT {{ vars.nope }} AS n")
    result = await SqlStepExecutor(connections=connections).execute(
        step=_step(), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "failed to render" in (result.error_message or "")


async def test_jinja_sandbox_blocks_filesystem_access(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    # A sandbox escape attempt in the file body is a render error, not an escape.
    _write_sql(paths, "q.sql", "SELECT {{ ''.__class__.__mro__ }}")
    result = await SqlStepExecutor(connections=connections).execute(
        step=_step(), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "failed to render" in (result.error_message or "")


# --- path confinement ------------------------------------------------------


async def test_path_traversal_is_rejected(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    result = await SqlStepExecutor(connections=connections).execute(
        step=_step(file="../../etc/passwd"), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "outside the project root" in (result.error_message or "")


async def test_missing_file_is_failed(paths: ProjectPaths, connections: ConnectionsConfig) -> None:
    result = await SqlStepExecutor(connections=connections).execute(
        step=_step(file="sql/nope.sql"), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "not found" in (result.error_message or "")


# --- connection / execution failures ---------------------------------------


async def test_unresolvable_connection_is_failed(paths: ProjectPaths) -> None:
    _write_sql(paths, "q.sql", "SELECT 1")
    empty = ConnectionsConfig()
    result = await SqlStepExecutor(connections=empty).execute(
        step=_step(connection="nope"), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "No connection named" in (result.error_message or "")


async def test_bad_sql_is_failed(paths: ProjectPaths, connections: ConnectionsConfig) -> None:
    _write_sql(paths, "q.sql", "SELECT * FROM no_such_table")
    result = await SqlStepExecutor(connections=connections).execute(
        step=_step(), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "sql execution failed" in (result.error_message or "")


# --- FIX-S1: JSON-serializable outputs -------------------------------------


async def test_outputs_are_json_serializable_for_exotic_types(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    # DuckDB returns datetime.date / datetime / Decimal native objects; raw,
    # they would break the JSONB persistence the runtime does. The executor
    # coerces them to JSON primitives before they enter outputs.
    _write_sql(
        paths,
        "q.sql",
        "SELECT DATE '2026-06-24' AS d, "
        "TIMESTAMP '2026-06-24 10:30:00' AS ts, "
        "CAST(1.50 AS DECIMAL(10, 2)) AS amt",
    )
    result = await SqlStepExecutor(connections=connections).execute(
        step=_step(), run=PipelineRun(pipeline="p"), paths=paths
    )

    assert result.status == "succeeded"
    # The whole outputs dict round-trips through json.dumps without error.
    json.dumps(result.outputs)
    row = result.outputs["rows"][0]
    assert row["d"] == "2026-06-24"
    assert row["ts"] == "2026-06-24T10:30:00"
    # Decimal → str (exact); not a float that would drop precision.
    assert row["amt"] == "1.50"
    assert isinstance(row["amt"], str)


# --- FIX-S2: the connection is closed --------------------------------------


class _RecordingConn:
    """A stub Connection that records close() and returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.closed = False

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
        return self._rows[:limit]

    def execute(self, sql: str) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _stub_factory(conn: Any) -> Any:
    def _factory(_name: str, _config: ConnectionsConfig) -> ResolvedConnection:
        from carve.core.connectors.duckdb import DIALECT

        return ResolvedConnection(conn, DIALECT)

    return _factory


async def test_connection_is_closed_after_a_successful_step(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    conn = _RecordingConn([{"n": 1}])
    _write_sql(paths, "q.sql", "SELECT 1 AS n")
    executor = SqlStepExecutor(connections=connections, connection_factory=_stub_factory(conn))

    result = await executor.execute(step=_step(), run=PipelineRun(pipeline="p"), paths=paths)

    assert result.status == "succeeded"
    assert conn.closed is True


async def test_connection_is_closed_when_the_step_fails(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    class _BoomConn(_RecordingConn):
        def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

    conn = _BoomConn([])
    _write_sql(paths, "q.sql", "SELECT 1 AS n")
    executor = SqlStepExecutor(connections=connections, connection_factory=_stub_factory(conn))

    result = await executor.execute(step=_step(), run=PipelineRun(pipeline="p"), paths=paths)

    assert result.status == "failed"
    assert conn.closed is True


# --- FIX-S3: per-step timeout enforced -------------------------------------


async def test_slow_step_times_out_and_is_failed(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    import time as _time

    class _SlowConn(_RecordingConn):
        def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
            _time.sleep(0.5)
            return [{"n": 1}]

    conn = _SlowConn([])
    _write_sql(paths, "q.sql", "SELECT 1 AS n")
    # A tiny timeout the slow connection blows past.
    executor = SqlStepExecutor(
        connections=connections, connection_factory=_stub_factory(conn), timeout_seconds=0
    )

    result = await executor.execute(step=_step(), run=PipelineRun(pipeline="p"), paths=paths)

    assert result.status == "failed"
    assert "timed out after 0s" in (result.error_message or "")


# --- FIX-S2 residual: no connection leak on the timeout path ---------------


async def test_timeout_does_not_leak_the_connection_real_duckdb(
    paths: ProjectPaths, connections: ConnectionsConfig
) -> None:
    # Reproduces the verifier's trace with the REAL DuckDBConnection + the REAL
    # ResolvedConnection (no stub): the connectors open the session LAZILY on the
    # first query, INSIDE the worker thread. asyncio.wait_for can't cancel a
    # to_thread worker, so the OLD main-thread `with resolved:` closed the
    # session on the main thread WHILE the worker was mid-query; the worker then
    # re-opened (or held) the session and never closed it → a leaked session.
    # The fix moves open+close INTO the worker, so the orphan closes the session
    # itself when it finishes.
    #
    # The handshake makes the repro deterministic (no sleep races): the worker
    # opens the real session, signals ``opened``, then BLOCKS on ``proceed``. The
    # tiny timeout fires while the worker is blocked (the session is genuinely
    # live), the step fails, then the test releases ``proceed`` and asserts the
    # worker closed the session. (Against the OLD code this assertion FAILS — the
    # session stays open — which is what makes this a true reproduce-test.)
    import asyncio
    import threading

    from carve.core.connectors.duckdb import DIALECT, DuckDBConnection

    opened = threading.Event()
    proceed = threading.Event()

    class _BlockingRealConn(DuckDBConnection):
        def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
            self._connect()  # open the REAL session inside the worker thread
            assert self._conn is not None  # session is live now
            opened.set()  # tell the test the session is open
            proceed.wait(timeout=5.0)  # block past the tiny step timeout
            return super().run_query(sql, limit=limit)

    real_conn = _BlockingRealConn(database=":memory:")
    resolved = ResolvedConnection(real_conn, DIALECT)

    def _factory(_name: str, _config: ConnectionsConfig) -> ResolvedConnection:
        return resolved

    _write_sql(paths, "q.sql", "SELECT 1 AS n")
    # A 1s budget: long enough for the worker to start + open the session,
    # short enough that the blocked worker blows past it (the worker waits 5s).
    executor = SqlStepExecutor(
        connections=connections, connection_factory=_factory, timeout_seconds=1
    )

    result = await executor.execute(step=_step(), run=PipelineRun(pipeline="p"), paths=paths)

    # The worker actually ran and opened the session before the timeout fired —
    # otherwise the leak can't manifest and the assertion below is vacuous.
    assert opened.is_set(), "worker never opened the session — repro is vacuous"
    # The step fails on the timeout (the await is abandoned)...
    assert result.status == "failed"
    assert "timed out after 1s" in (result.error_message or "")
    # The real session is still LIVE here — the worker is blocked, the timeout
    # already fired. The OLD code would have closed it on the main thread; the
    # NEW code leaves the worker to own its lifecycle.
    assert real_conn._conn is not None

    # Release the worker; it returns from run_query and its own `with resolved:`
    # closes the session. Poll until closed (close() sets _conn back to None).
    proceed.set()
    for _ in range(100):
        if real_conn._conn is None:
            break
        await asyncio.sleep(0.05)
    assert real_conn._conn is None, "the orphaned worker leaked the connection (not closed)"


# --- default timeout config ------------------------------------------------


def test_default_timeout_is_300s(connections: ConnectionsConfig) -> None:
    executor = SqlStepExecutor(connections=connections)
    assert executor._timeout_seconds == 300


def test_resolve_connection_errors_on_unknown_name() -> None:
    with pytest.raises(ConnectionResolutionError):
        from carve.runtime.step_types.connections import resolve_connection

        resolve_connection("nope", ConnectionsConfig())
