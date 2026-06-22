"""Per-dialect `INFORMATION_SCHEMA` introspection — read the real schema.

Dialect-dispatched list/describe/exists so agents ground on the actual schema,
never a guessed one. Snowflake + DuckDB are wired here; the four author-only
dialects raise :class:`UnsupportedIntrospectionError` (they can still
validate/transpile, just not introspect a live catalog yet).

Identifier safety: database/schema/table names are validated as plain
identifiers before any is interpolated into a ``FROM`` clause (the drivers bind
*values*, not identifiers); values are always bound, never interpolated.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol

from carve.core.sql.dialects import normalize_dialect

# Reuse the catalog caps so introspect matches the legacy skill behavior.
LIST_SCHEMAS_CAP = 100
LIST_TABLES_CAP = 200

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class IntrospectQueryRunner(Protocol):
    """The minimal read surface introspection needs (Snowflake + DuckDB both have it)."""

    def query(
        self, sql: str, params: dict[str, Any] | list[Any] | None = None
    ) -> list[dict[str, Any]]: ...


class InvalidIdentifierError(ValueError):
    """A database/schema/table name is not a plain unquoted identifier."""


class UnsupportedIntrospectionError(ValueError):
    """Introspection isn't wired for this dialect (author-only dialect)."""


def _ident(value: str, *, what: str) -> str:
    if not _IDENT_RE.fullmatch(value):
        raise InvalidIdentifierError(
            f"{what} {value!r} is not a plain identifier (letters, digits, "
            "underscores; must start with a letter or underscore)."
        )
    return value


def _duckdb_catalog(runner: IntrospectQueryRunner, database: str | None) -> str:
    """The catalog to scope DuckDB introspection to.

    DuckDB's ``information_schema`` spans every *attached* catalog (memory,
    system, temp), so an unscoped query double-counts (three ``main`` schemas).
    Default to the connection's current catalog. Bound as a value, never
    interpolated — no identifier-injection surface.
    """
    if database is not None:
        return database
    rows = runner.query("SELECT current_database() AS db")
    return str(rows[0]["db"]) if rows else "memory"


def list_schemas(
    runner: IntrospectQueryRunner, dialect: str, *, database: str | None = None
) -> dict[str, Any]:
    name = normalize_dialect(dialect)
    if name == "snowflake":
        db = _ident(database, what="database") if database else None
        prefix = f"{db}." if db else ""
        rows = runner.query(
            f"SELECT schema_name FROM {prefix}information_schema.schemata "
            f"WHERE schema_name <> 'INFORMATION_SCHEMA' ORDER BY schema_name "
            f"LIMIT {LIST_SCHEMAS_CAP + 1}"
        )
    elif name == "duckdb":
        rows = runner.query(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE catalog_name = ? "
            "AND schema_name NOT IN ('information_schema', 'pg_catalog') "
            f"ORDER BY schema_name LIMIT {LIST_SCHEMAS_CAP + 1}",
            [_duckdb_catalog(runner, database)],
        )
    else:
        raise UnsupportedIntrospectionError(_unsupported(name))
    return _capped(rows, LIST_SCHEMAS_CAP)


def list_tables(
    runner: IntrospectQueryRunner,
    dialect: str,
    *,
    schema: str,
    database: str | None = None,
    include_views: bool = True,
) -> dict[str, Any]:
    name = normalize_dialect(dialect)
    type_filter = "" if include_views else " AND table_type = 'BASE TABLE'"
    if name == "snowflake":
        db = _ident(database, what="database") if database else None
        prefix = f"{db}." if db else ""
        rows = runner.query(
            f"SELECT table_name, table_type FROM {prefix}information_schema.tables "
            f"WHERE table_schema = %(schema)s{type_filter} "
            f"ORDER BY table_name LIMIT {LIST_TABLES_CAP + 1}",
            {"schema": schema.upper()},
        )
    elif name == "duckdb":
        rows = runner.query(
            "SELECT table_name, table_type FROM information_schema.tables "
            f"WHERE table_catalog = ? AND table_schema = ?{type_filter} "
            f"ORDER BY table_name LIMIT {LIST_TABLES_CAP + 1}",
            [_duckdb_catalog(runner, database), schema],
        )
    else:
        raise UnsupportedIntrospectionError(_unsupported(name))
    return _capped(rows, LIST_TABLES_CAP)


def describe_table(
    runner: IntrospectQueryRunner,
    dialect: str,
    *,
    schema: str,
    table: str,
    database: str | None = None,
) -> dict[str, Any]:
    name = normalize_dialect(dialect)
    if name == "snowflake":
        db = _ident(database, what="database") if database else None
        prefix = f"{db}." if db else ""
        rows = runner.query(
            f"SELECT column_name, data_type, is_nullable, ordinal_position "
            f"FROM {prefix}information_schema.columns "
            f"WHERE table_schema = %(schema)s AND table_name = %(table)s "
            f"ORDER BY ordinal_position",
            {"schema": schema.upper(), "table": table.upper()},
        )
    elif name == "duckdb":
        rows = runner.query(
            "SELECT column_name, data_type, is_nullable, ordinal_position "
            "FROM information_schema.columns "
            "WHERE table_catalog = ? AND table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [_duckdb_catalog(runner, database), schema, table],
        )
    else:
        raise UnsupportedIntrospectionError(_unsupported(name))
    return {"rows": rows, "truncated": False, "total_count": len(rows)}


def table_exists(
    runner: IntrospectQueryRunner,
    dialect: str,
    *,
    schema: str,
    table: str,
    database: str | None = None,
) -> dict[str, Any]:
    described = describe_table(runner, dialect, schema=schema, table=table, database=database)
    return {"exists": len(described["rows"]) > 0}


def _capped(rows: list[dict[str, Any]], cap: int) -> dict[str, Any]:
    truncated = len(rows) > cap
    return {
        "rows": rows[:cap] if truncated else rows,
        "truncated": truncated,
        "total_count": None if truncated else len(rows),
    }


def _unsupported(dialect: str) -> str:
    return (
        f"Introspection is not wired for dialect {dialect!r} yet "
        "(author-only — validate/transpile work, live catalog reads don't)."
    )


# kind → callable, for the sql tool's `introspect` op dispatch. The callables
# have different keyword params; the tool passes only the supplied kwargs and
# surfaces a missing-arg TypeError as a clean tool error.
INTROSPECT_OPS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_schemas": list_schemas,
    "list_tables": list_tables,
    "describe_table": describe_table,
    "table_exists": table_exists,
}

__all__ = [
    "INTROSPECT_OPS",
    "LIST_SCHEMAS_CAP",
    "LIST_TABLES_CAP",
    "IntrospectQueryRunner",
    "InvalidIdentifierError",
    "UnsupportedIntrospectionError",
    "describe_table",
    "list_schemas",
    "list_tables",
    "table_exists",
]
