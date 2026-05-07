"""Unit tests for the five built-in catalog skills.

Each test injects a fake `SnowflakeConnection`-like object (records the
SQL it sees, returns a canned result set) into a `SnowflakePool` stub,
runs the skill via `CachedSkillExecutor`, and asserts on the resulting
`SkillResult`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
)
from carve.core.config.schema import SnowflakeConnection as ConnConfig
from carve.core.skills import load_builtin_skills
from carve.core.skills.context import SkillContext
from carve.core.skills.executor import CachedSkillExecutor

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSnowflake:
    """Fake `SnowflakeConnection` that records calls and returns canned rows."""

    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        # Real config attribute so skills can read account/role if needed.
        self.config = ConnConfig(
            account="x",
            user="u",
            password="p",
            role="R",
            warehouse="W",
            database="DB",
        )

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if not self._responses:
            return []
        return self._responses.pop(0)


class _FakePool:
    def __init__(self, by_target: dict[str, _FakeSnowflake]) -> None:
        self._by_target = by_target

    def get(self, target: str) -> _FakeSnowflake:
        return self._by_target[target]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config() -> Config:
    return Config(
        project=ProjectConfig(name="t"),
        connections=ConnectionsConfig(snowflake={}),
        models=ModelsConfig(anthropic_api_key="x"),
    )


def _ctx(sf: _FakeSnowflake, target: str = "dev") -> SkillContext:
    pool = _FakePool({target: sf})
    return SkillContext(
        config=_config(),
        repo=MagicMock(),
        run_id=None,
        target=target,
        snowflake_pool=pool,  # type: ignore[arg-type]
    )


@pytest.fixture
def registry() -> Any:
    return load_builtin_skills()


# ---------------------------------------------------------------------------
# list_databases
# ---------------------------------------------------------------------------


def test_list_databases(registry: Any) -> None:
    """3 databases fixture → all three returned with the expected shape."""
    rows = [
        {"DATABASE_NAME": "ANALYTICS", "DATABASE_OWNER": "SYSADMIN", "CREATED": "2024-01-01"},
        {"DATABASE_NAME": "RAW", "DATABASE_OWNER": "SYSADMIN", "CREATED": "2024-01-02"},
        {"DATABASE_NAME": "STAGING", "DATABASE_OWNER": "SYSADMIN", "CREATED": "2024-01-03"},
    ]
    sf = _FakeSnowflake([rows])
    executor = CachedSkillExecutor(registry)

    result = executor.execute("list_databases", {}, _ctx(sf))

    assert result.truncated is False
    assert result.total_count == 3
    assert isinstance(result.data, dict)
    assert [row["DATABASE_NAME"] for row in result.data["databases"]] == [
        "ANALYTICS",
        "RAW",
        "STAGING",
    ]
    sql, _params = sf.calls[0]
    assert "information_schema.databases" in sql.lower()


# ---------------------------------------------------------------------------
# list_tables
# ---------------------------------------------------------------------------


def _table_row(idx: int) -> dict[str, Any]:
    return {
        "TABLE_NAME": f"T_{idx:04d}",
        "TABLE_TYPE": "BASE TABLE",
        "ROW_COUNT": idx * 10,
        "BYTES": idx * 1000,
    }


def test_list_tables_truncates_at_200(registry: Any) -> None:
    """250 fixture rows → 200 returned with `truncated=True, total_count=250`."""
    rows = [_table_row(i) for i in range(250)]
    # First call returns the 250 rows; second call (the COUNT(*) followup)
    # returns the total.
    sf = _FakeSnowflake([rows, [{"N": 250}]])
    executor = CachedSkillExecutor(registry)

    result = executor.execute(
        "list_tables",
        {"database": "RAW", "schema": "public"},
        _ctx(sf),
    )

    assert result.truncated is True
    assert result.total_count == 250
    assert len(result.data["tables"]) == 200
    # Schema is upper-cased before binding.
    assert sf.calls[0][1] == {"schema": "PUBLIC"}


def test_list_tables_excludes_views_when_requested(registry: Any) -> None:
    """`include_views=False` adds the `BASE TABLE` filter to the SQL."""
    sf = _FakeSnowflake([[]])
    executor = CachedSkillExecutor(registry)
    executor.execute(
        "list_tables",
        {"database": "RAW", "schema": "public", "include_views": False},
        _ctx(sf),
    )
    sql = sf.calls[0][0]
    assert "BASE TABLE" in sql.upper()


def test_list_tables_no_truncation_below_cap(registry: Any) -> None:
    """A small schema returns every row with `truncated=False`."""
    rows = [_table_row(i) for i in range(5)]
    sf = _FakeSnowflake([rows])
    executor = CachedSkillExecutor(registry)
    result = executor.execute(
        "list_tables",
        {"database": "RAW", "schema": "public"},
        _ctx(sf),
    )
    assert result.truncated is False
    assert result.total_count == 5
    assert len(result.data["tables"]) == 5


# ---------------------------------------------------------------------------
# describe_table
# ---------------------------------------------------------------------------


def test_describe_table_returns_typed_columns(registry: Any) -> None:
    """Column rows include name, type, nullability, and ordinal."""
    rows = [
        {
            "COLUMN_NAME": "ID",
            "DATA_TYPE": "NUMBER",
            "IS_NULLABLE": "NO",
            "ORDINAL_POSITION": 1,
        },
        {
            "COLUMN_NAME": "NAME",
            "DATA_TYPE": "TEXT",
            "IS_NULLABLE": "YES",
            "ORDINAL_POSITION": 2,
        },
    ]
    sf = _FakeSnowflake([rows])
    executor = CachedSkillExecutor(registry)
    result = executor.execute(
        "describe_table",
        {"database": "RAW", "schema": "raw", "table": "events"},
        _ctx(sf),
    )
    assert result.total_count == 2
    cols = result.data["columns"]
    assert cols[0]["COLUMN_NAME"] == "ID"
    assert cols[0]["DATA_TYPE"] == "NUMBER"
    assert cols[1]["IS_NULLABLE"] == "YES"
    # The skill upper-cases the schema/table args before binding.
    assert sf.calls[0][1] == {"schema": "RAW", "table": "EVENTS"}


# ---------------------------------------------------------------------------
# table_exists
# ---------------------------------------------------------------------------


def test_table_exists_true_false_paths(registry: Any) -> None:
    """COUNT(*) > 0 → True; COUNT(*) = 0 → False."""
    sf = _FakeSnowflake([[{"N": 1}], [{"N": 0}]])
    executor1 = CachedSkillExecutor(registry)
    executor2 = CachedSkillExecutor(registry)

    yes = executor1.execute(
        "table_exists",
        {"database": "RAW", "schema": "raw", "table": "events"},
        _ctx(sf),
    )
    no = executor2.execute(
        "table_exists",
        {"database": "RAW", "schema": "raw", "table": "missing"},
        _ctx(sf),
    )
    assert yes.data == {"exists": True}
    assert no.data == {"exists": False}


# ---------------------------------------------------------------------------
# database-name validation (defense against SQL injection via LLM-supplied
# database names — see InvalidDatabaseNameError).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "skill_name,kwargs",
    [
        ("list_schemas", {"database": "x.foo"}),
        ("list_schemas", {"database": "x; DROP TABLE y"}),
        ("list_schemas", {"database": "x UNION SELECT 1"}),
        ("list_tables", {"database": '"quoted"', "schema": "s"}),
        ("describe_table", {"database": "x.foo", "schema": "s", "table": "t"}),
        ("table_exists", {"database": "1starts_digit", "schema": "s", "table": "t"}),
    ],
)
def test_catalog_skills_reject_unsafe_database_names(
    registry: Any, skill_name: str, kwargs: dict[str, Any]
) -> None:
    """Anything other than ``[A-Za-z_][A-Za-z0-9_]*`` raises before SQL runs."""
    from carve.core.skills.builtin.catalog import InvalidDatabaseNameError

    sf = _FakeSnowflake([])
    executor = CachedSkillExecutor(registry)
    with pytest.raises(InvalidDatabaseNameError):
        executor.execute(skill_name, kwargs, _ctx(sf))
    # No SQL was issued — the validator runs before pool.get().
    assert sf.calls == []
