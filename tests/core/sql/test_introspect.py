"""Dialect-dispatched introspection against an in-memory DuckDB."""

from __future__ import annotations

import pytest

from carve.core.connectors.duckdb import DuckDBConnection
from carve.core.sql.introspect import (
    InvalidIdentifierError,
    UnsupportedIntrospectionError,
    describe_table,
    list_schemas,
    list_tables,
    table_exists,
)


@pytest.fixture
def con() -> DuckDBConnection:
    c = DuckDBConnection(":memory:")
    c.execute("CREATE TABLE orders (id INTEGER, total DECIMAL(10,2))")
    c.execute("CREATE VIEW order_ids AS SELECT id FROM orders")
    return c


def test_list_schemas_includes_main(con: DuckDBConnection) -> None:
    result = list_schemas(con, "duckdb")
    names = {r["schema_name"] for r in result["rows"]}
    assert "main" in names
    assert result["truncated"] is False


def test_list_schemas_scopes_to_one_catalog(con: DuckDBConnection) -> None:
    # DuckDB's information_schema spans attached catalogs (memory/system/temp);
    # without a catalog filter 'main' appears 3 times. The fix scopes to the
    # connection's current catalog.
    result = list_schemas(con, "duckdb")
    mains = [r for r in result["rows"] if r["schema_name"] == "main"]
    assert len(mains) == 1


def test_list_tables(con: DuckDBConnection) -> None:
    result = list_tables(con, "duckdb", schema="main")
    names = {r["table_name"] for r in result["rows"]}
    assert {"orders", "order_ids"} <= names


def test_list_tables_excludes_views_when_asked(con: DuckDBConnection) -> None:
    result = list_tables(con, "duckdb", schema="main", include_views=False)
    names = {r["table_name"] for r in result["rows"]}
    assert "orders" in names
    assert "order_ids" not in names


def test_describe_table(con: DuckDBConnection) -> None:
    result = describe_table(con, "duckdb", schema="main", table="orders")
    cols = {r["column_name"] for r in result["rows"]}
    assert cols == {"id", "total"}
    assert result["total_count"] == 2


def test_table_exists(con: DuckDBConnection) -> None:
    assert table_exists(con, "duckdb", schema="main", table="orders") == {"exists": True}
    assert table_exists(con, "duckdb", schema="main", table="ghost") == {"exists": False}


def test_unsupported_dialect_raises(con: DuckDBConnection) -> None:
    with pytest.raises(UnsupportedIntrospectionError):
        list_schemas(con, "bigquery")


def test_snowflake_database_identifier_is_validated(con: DuckDBConnection) -> None:
    # The snowflake path interpolates the database into FROM; a non-identifier
    # must be rejected before any query is built.
    with pytest.raises(InvalidIdentifierError):
        list_tables(con, "snowflake", schema="public", database="db; DROP TABLE x")
