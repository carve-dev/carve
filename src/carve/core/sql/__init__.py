"""Dialect-aware SQL: a shared tool layer every agent uses.

`sqlglot`-backed validation/transpile/classification, per-dialect
`INFORMATION_SCHEMA` introspection, and permission-gated execution. Snowflake +
DuckDB are first-class (DuckDB powers local dev + tests); the other four
dialects (postgres/bigquery/databricks/sqlserver) get author-time
validate/transpile via `sqlglot` but no first-class run/introspect adapter yet.

Lean first pass (see DELIVERY): the `sql` tool (ops `validate`/`transpile`/
`introspect`/`run`), the sqlglot statement classifier, DuckDB + Snowflake
runners, and dialect-dispatched introspection. Warehouse writes/DDL are gated
to **deploy** mode on the write role (the shipped `warehouse_roles` floor);
`generate`/`modify`/`explain` are the SQL specialist's LLM job, not tool ops.
"""

from __future__ import annotations

from carve.core.sql.classify import (
    SqlClassificationError,
    StatementKind,
    classify,
    is_read_only,
)
from carve.core.sql.dialects import (
    UnsupportedDialectError,
    normalize_dialect,
    transpile,
    validate,
)

__all__ = [
    "SqlClassificationError",
    "StatementKind",
    "UnsupportedDialectError",
    "classify",
    "is_read_only",
    "normalize_dialect",
    "transpile",
    "validate",
]
