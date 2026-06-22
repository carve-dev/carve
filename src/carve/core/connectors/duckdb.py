"""DuckDB connector — the first-class local-dev + test substrate.

DuckDB is an in-process engine (no server, no creds), so it makes the whole SQL
stack runnable locally and in CI without a warehouse. The connection exposes the
same minimal surface the agent tools use against Snowflake — ``query`` (dict
rows), ``run_query`` (capped read for the agent tool), and ``execute`` (writes /
DDL) — so the dialect-aware ``sql`` tool treats both uniformly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb

DIALECT = "duckdb"


class DuckDBConnection:
    """A lazily-opened DuckDB connection (in-memory by default)."""

    dialect = DIALECT

    def __init__(self, database: str = ":memory:") -> None:
        self.database = database
        self._conn: duckdb.DuckDBPyConnection | None = None

    def _connect(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            import duckdb

            self._conn = duckdb.connect(self.database)
        return self._conn

    def query(
        self, sql: str, params: dict[str, Any] | list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run ``sql`` and return rows as dicts (column name → value)."""
        cursor = self._connect().execute(sql, params or [])
        if cursor.description is None:
            return []
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
        """Protocol-compliant read entry point for the agent's sql tool.

        Caps the result to ``limit`` rows (DuckDB is local, so over-fetch +
        slice is cheap and avoids rewriting the user's SQL).
        """
        rows = self.query(sql)
        return rows[:limit]

    def execute(self, sql: str) -> None:
        """Run a write / DDL statement (no result rows)."""
        self._connect().execute(sql)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


__all__ = ["DIALECT", "DuckDBConnection"]
