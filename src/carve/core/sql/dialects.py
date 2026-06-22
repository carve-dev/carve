"""Dialect resolution + `sqlglot`-backed validate / transpile.

The dialect is resolved from a connection (snowflake / duckdb first-class;
postgres / bigquery / databricks / sqlserver author-only). `validate` parses
against the dialect and returns parse errors *before* anything runs (grounding);
`transpile` rewrites a query from one dialect to another so an agent can author
once and target the connection's dialect.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot.errors import SqlglotError

# Dialects Carve recognizes. snowflake + duckdb are first-class (run +
# introspect); the rest are author-only (validate/transpile work via sqlglot,
# but there's no first-class run/introspect adapter yet).
FIRST_CLASS_DIALECTS: frozenset[str] = frozenset({"snowflake", "duckdb"})
AUTHOR_ONLY_DIALECTS: frozenset[str] = frozenset({"postgres", "bigquery", "databricks", "tsql"})
SUPPORTED_DIALECTS: frozenset[str] = FIRST_CLASS_DIALECTS | AUTHOR_ONLY_DIALECTS

# Friendly aliases → the sqlglot dialect name.
_ALIASES = {
    "postgresql": "postgres",
    "pg": "postgres",
    "sqlserver": "tsql",
    "mssql": "tsql",
    "bq": "bigquery",
}


class UnsupportedDialectError(ValueError):
    """The requested dialect is not one Carve recognizes."""


def normalize_dialect(dialect: str) -> str:
    """Return the canonical sqlglot dialect name, or raise."""
    name = _ALIASES.get(dialect.strip().lower(), dialect.strip().lower())
    if name not in SUPPORTED_DIALECTS:
        raise UnsupportedDialectError(
            f"Unsupported SQL dialect {dialect!r}. "
            f"Supported: {', '.join(sorted(SUPPORTED_DIALECTS))}."
        )
    return name


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating one SQL string against a dialect."""

    ok: bool
    error: str | None = None


def validate(sql: str, dialect: str) -> ValidationResult:
    """Parse ``sql`` against ``dialect``; report parse errors without running."""
    name = normalize_dialect(dialect)
    try:
        statements = sqlglot.parse(sql, read=name)
    except SqlglotError as exc:
        return ValidationResult(ok=False, error=str(exc))
    if not any(s is not None for s in statements):
        return ValidationResult(ok=False, error="No SQL statement found.")
    return ValidationResult(ok=True)


def transpile(sql: str, *, read: str, write: str) -> list[str]:
    """Rewrite ``sql`` from the ``read`` dialect to the ``write`` dialect.

    Raises :class:`UnsupportedDialectError` for an unknown dialect and
    :class:`sqlglot.errors.SqlglotError` (re-raised) on a parse failure, so the
    caller can surface a precise authoring error.
    """
    read_name = normalize_dialect(read)
    write_name = normalize_dialect(write)
    return sqlglot.transpile(sql, read=read_name, write=write_name)


__all__ = [
    "AUTHOR_ONLY_DIALECTS",
    "FIRST_CLASS_DIALECTS",
    "SUPPORTED_DIALECTS",
    "UnsupportedDialectError",
    "ValidationResult",
    "normalize_dialect",
    "transpile",
    "validate",
]
