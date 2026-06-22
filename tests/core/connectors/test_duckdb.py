"""DuckDBConnection — query / run_query / execute roundtrip (in-memory)."""

from __future__ import annotations

from carve.core.connectors.duckdb import DuckDBConnection


def test_query_returns_dict_rows() -> None:
    con = DuckDBConnection(":memory:")
    con.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    con.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
    rows = con.query("SELECT id, name FROM t ORDER BY id")
    assert rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_run_query_caps_rows() -> None:
    con = DuckDBConnection(":memory:")
    con.execute("CREATE TABLE t (id INTEGER)")
    con.execute("INSERT INTO t VALUES (1), (2), (3)")
    assert con.run_query("SELECT id FROM t ORDER BY id", limit=2) == [{"id": 1}, {"id": 2}]


def test_params_are_bound_positionally() -> None:
    con = DuckDBConnection(":memory:")
    con.execute("CREATE TABLE t (id INTEGER)")
    con.execute("INSERT INTO t VALUES (1), (2), (3)")
    rows = con.query("SELECT id FROM t WHERE id > ?", [1])
    assert {r["id"] for r in rows} == {2, 3}


def test_execute_no_rows_returns_empty() -> None:
    con = DuckDBConnection(":memory:")
    assert con.query("CREATE TABLE t (id INTEGER)") == []


def test_dialect_attribute() -> None:
    assert DuckDBConnection(":memory:").dialect == "duckdb"
