"""The `sql` tool: ops + permission/role-gated execution against DuckDB."""

from __future__ import annotations

import pytest

from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.tools import ToolExecutionError
from carve.core.connectors.duckdb import DuckDBConnection
from carve.core.sql.tool import make_sql_tool


@pytest.fixture
def con() -> DuckDBConnection:
    c = DuckDBConnection(":memory:")
    c.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    c.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
    return c


def _tool(con: DuckDBConnection, mode: PermissionMode, **kw):
    return make_sql_tool(dialect="duckdb", mode=mode, read_runner=con, **kw)


def test_validate_op(con: DuckDBConnection) -> None:
    tool = _tool(con, PermissionMode.READ_ONLY)
    assert tool.executor({"op": "validate", "sql": "SELECT * FROM t"}) == {
        "ok": True,
        "error": None,
    }
    bad = tool.executor({"op": "validate", "sql": "SELECT FROM WHERE"})
    assert bad["ok"] is False


def test_transpile_op(con: DuckDBConnection) -> None:
    tool = _tool(con, PermissionMode.READ_ONLY)
    out = tool.executor({"op": "transpile", "sql": "SELECT 1", "to_dialect": "snowflake"})
    assert out.get("sql")


def test_run_read(con: DuckDBConnection) -> None:
    tool = _tool(con, PermissionMode.READ_ONLY)
    result = tool.executor({"op": "run", "sql": "SELECT * FROM t ORDER BY id"})
    assert result["row_count"] == 2
    assert result["rows"][0]["name"] == "a"


def test_run_caps_rows(con: DuckDBConnection) -> None:
    con.execute("INSERT INTO t VALUES (3, 'c'), (4, 'd'), (5, 'e')")
    tool = _tool(con, PermissionMode.READ_ONLY, row_cap=2)
    result = tool.executor({"op": "run", "sql": "SELECT * FROM t"})
    assert result["row_count"] == 2
    assert result["truncated"] is True


def test_run_introspect(con: DuckDBConnection) -> None:
    tool = _tool(con, PermissionMode.READ_ONLY)
    result = tool.executor(
        {"op": "introspect", "kind": "describe_table", "schema": "main", "table": "t"}
    )
    assert {r["column_name"] for r in result["rows"]} == {"id", "name"}


def test_write_denied_below_deploy(con: DuckDBConnection) -> None:
    for mode in (PermissionMode.READ_ONLY, PermissionMode.PLAN, PermissionMode.BUILD):
        tool = _tool(con, mode, write_runner=con)
        with pytest.raises(ToolExecutionError):
            tool.executor({"op": "run", "sql": "DELETE FROM t"})
    # The row is untouched.
    assert con.run_query("SELECT COUNT(*) AS n FROM t", limit=1)[0]["n"] == 2


def test_write_allowed_in_deploy_with_write_runner(con: DuckDBConnection) -> None:
    tool = _tool(con, PermissionMode.DEPLOY, write_runner=con)
    result = tool.executor({"op": "run", "sql": "DELETE FROM t WHERE id = 1"})
    assert result == {"executed": True, "kind": "write"}
    assert con.run_query("SELECT COUNT(*) AS n FROM t", limit=1)[0]["n"] == 1


def test_write_in_deploy_needs_write_runner(con: DuckDBConnection) -> None:
    tool = _tool(con, PermissionMode.DEPLOY)  # no write_runner
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "run", "sql": "DELETE FROM t"})


def test_destructive_ddl_requires_approval(con: DuckDBConnection) -> None:
    # Deploy + write runner, but no approver → destructive DDL denied.
    denied = _tool(con, PermissionMode.DEPLOY, write_runner=con)
    with pytest.raises(ToolExecutionError):
        denied.executor({"op": "run", "sql": "DROP TABLE t"})
    # With an approver that says yes → executes.
    approved = _tool(con, PermissionMode.DEPLOY, write_runner=con, approver=lambda _msg: True)
    assert approved.executor({"op": "run", "sql": "DROP TABLE t"})["executed"] is True


def test_unknown_op_and_missing_sql(con: DuckDBConnection) -> None:
    tool = _tool(con, PermissionMode.READ_ONLY)
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "frobnicate"})
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "run", "sql": "  "})


def test_unparseable_run_is_refused(con: DuckDBConnection) -> None:
    tool = _tool(con, PermissionMode.READ_ONLY)
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "run", "sql": "this is (not sql"})


def test_exact_cap_is_not_reported_truncated(con: DuckDBConnection) -> None:
    con.execute("INSERT INTO t VALUES (3, 'c')")  # exactly 3 rows now
    tool = _tool(con, PermissionMode.READ_ONLY, row_cap=3)
    result = tool.executor({"op": "run", "sql": "SELECT * FROM t"})
    assert result["row_count"] == 3
    assert result["truncated"] is False  # all rows present; over-fetch found no extra


def test_select_into_in_read_only_never_reaches_the_read_runner() -> None:
    # Pins the BLOCKER fix: a SELECT ... INTO write must be denied, not
    # dispatched to the read runner, in read_only mode.
    calls: list[str] = []

    class _Recording:
        def run_query(self, sql: str, *, limit: int) -> list[dict[str, object]]:
            calls.append(sql)
            return []

        def query(self, sql: str, params: object = None) -> list[dict[str, object]]:
            return []

    tool = make_sql_tool(
        dialect="snowflake", mode=PermissionMode.READ_ONLY, read_runner=_Recording()
    )
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "run", "sql": "SELECT * INTO sneaky FROM secrets"})
    assert calls == []  # the write never reached the read runner
