"""Dialect normalization + sqlglot validate / transpile."""

from __future__ import annotations

import pytest

from carve.core.sql.dialects import (
    SUPPORTED_DIALECTS,
    UnsupportedDialectError,
    normalize_dialect,
    transpile,
    validate,
)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("snowflake", "snowflake"),
        ("DuckDB", "duckdb"),
        ("postgresql", "postgres"),
        ("pg", "postgres"),
        ("sqlserver", "tsql"),
        ("mssql", "tsql"),
        ("bq", "bigquery"),
    ],
)
def test_normalize_dialect_aliases(given: str, expected: str) -> None:
    assert normalize_dialect(given) == expected
    assert expected in SUPPORTED_DIALECTS


def test_normalize_dialect_rejects_unknown() -> None:
    with pytest.raises(UnsupportedDialectError):
        normalize_dialect("oracle")


def test_validate_ok_and_error() -> None:
    assert validate("SELECT 1", "duckdb").ok is True
    bad = validate("SELECT FROM WHERE", "duckdb")
    assert bad.ok is False
    assert bad.error


def test_validate_empty_is_not_ok() -> None:
    assert validate("   ", "snowflake").ok is False


def test_transpile_between_dialects() -> None:
    # DuckDB→Snowflake: a portable query round-trips; the call must not raise
    # and must return a statement.
    out = transpile("SELECT 1 AS x", read="duckdb", write="snowflake")
    assert out and "SELECT" in out[0].upper()


def test_transpile_unknown_dialect_raises() -> None:
    with pytest.raises(UnsupportedDialectError):
        transpile("SELECT 1", read="duckdb", write="oracle")
