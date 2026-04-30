# M1-06 — Snowflake connector

**Milestone:** 1 — Walking skeleton
**Estimated effort:** 0.5 day
**Dependencies:** M1-02 (config provides connection details)

## Purpose

Provide a thin, well-tested wrapper around `snowflake-connector-python` that the agent's tools (M1-04) and any internal code uses to talk to Snowflake. Centralizes connection management, query execution, and error handling so we don't repeat patterns across the codebase.

## Scope

### In scope

- `SnowflakeConnection` class wrapping the official connector
- Connection pool/cache keyed by target name
- Query execution with parameter binding
- Sensible error wrapping for common failure modes
- Read-only mode enforcement for the agent's `run_snowflake_query` tool
- Authentication via password, key-pair, and external browser

### Out of scope

- Async query execution (use sync; Snowflake's async API has gotchas we'll handle later)
- Snowpark integration (M3 or later if needed)
- Connection-level RBAC checks (handled by Snowflake itself)
- Result streaming for huge result sets (M3 for the `sql` step type)

## Implementation

> **Updated during implementation (2026-04-29):** `SnowflakeError` lives in its own `exceptions.py` (matching the file list below) rather than inside `snowflake.py`, and gained structured `hint` / `error_code` fields. `SnowflakeConnection` also exposes a `run_query(sql, *, limit) -> list[dict]` method to satisfy M1-04's `SnowflakeQueryRunner` Protocol; it delegates to `query()`. The `schema` config field is read as `schema_` to avoid the Pydantic shadow.

### File: `src/carve/core/connectors/exceptions.py`

```python
class SnowflakeError(Exception):
    """Wrapped Snowflake error with optional hint context."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.message = message
        self.hint = hint
        self.error_code = error_code
        super().__init__(self._render())

    def _render(self) -> str:
        if self.hint:
            return f"{self.message}\n  Hint: {self.hint}"
        return self.message
```

### File: `src/carve/core/connectors/snowflake.py`

```python
import snowflake.connector
from snowflake.connector import DictCursor
from carve.core.config.schema import SnowflakeConnection as ConnConfig
from carve.core.connectors.exceptions import SnowflakeError

class SnowflakeConnection:
    def __init__(self, config: ConnConfig):
        self.config = config
        self._connection = None

    def connect(self):
        if self._connection is not None:
            return self._connection

        kwargs = {
            "account": self.config.account,
            "user": self.config.user,
            "role": self.config.role,
            "warehouse": self.config.warehouse,
            "database": self.config.database,
            "schema": self.config.schema_ or "PUBLIC",
        }

        # Auth precedence: externalbrowser → key-pair → password → error.
        if self.config.authenticator == "externalbrowser":
            kwargs["authenticator"] = "externalbrowser"
        elif self.config.private_key_path:
            kwargs["private_key"] = self._load_private_key()
        elif self.config.password:
            kwargs["password"] = self.config.password
        else:
            raise SnowflakeError(
                "No authentication method configured.",
                hint="Provide password, private_key_path, or set authenticator='externalbrowser'.",
            )

        try:
            self._connection = snowflake.connector.connect(**kwargs)
        except snowflake.connector.errors.DatabaseError as e:
            message, hint, code = _format_error("", e)
            raise SnowflakeError(
                f"Failed to connect to Snowflake: {e}", hint=hint, error_code=code
            ) from e

        return self._connection

    def query(
        self,
        sql: str,
        params: dict | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        conn = self.connect()
        cursor = conn.cursor(DictCursor)
        try:
            final_sql = sql
            if limit is not None and not _has_limit(final_sql):
                # rstrip whitespace before stripping the trailing semicolon.
                final_sql = f"{final_sql.rstrip().rstrip(';')} LIMIT {int(limit)}"
            cursor.execute(final_sql, params or {})
            return [dict(row) for row in cursor.fetchall()]
        except snowflake.connector.errors.ProgrammingError as e:
            message, hint, code = _format_error(sql, e)
            raise SnowflakeError(message, hint=hint, error_code=code) from e
        finally:
            cursor.close()

    def execute(self, sql: str, params: dict | None = None) -> int:
        """Execute non-SELECT SQL. Returns rows affected."""
        conn = self.connect()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params or {})
            return cursor.rowcount or 0
        except snowflake.connector.errors.ProgrammingError as e:
            message, hint, code = _format_error(sql, e)
            raise SnowflakeError(message, hint=hint, error_code=code) from e
        finally:
            cursor.close()

    def run_query(self, sql: str, *, limit: int) -> list[dict]:
        """Protocol-compliant entry point for M1-04's `SnowflakeQueryRunner`.

        Delegates to `query()`; kept distinct so the keyword-only `limit`
        matches the Protocol declared in `carve.core.agents.m1_tools`.
        """
        return self.query(sql, limit=limit)

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
```

`_has_limit(sql)` checks the last ~8 whitespace-delimited tokens (after stripping trailing whitespace and `;`) for the literal `LIMIT`, so we don't double-append when the caller already supplied one.

### Connection pool

Connections are expensive to create. A simple pool keyed by target name:

```python
class SnowflakePool:
    def __init__(self, config: Config):
        self.config = config
        self._pool: dict[str, SnowflakeConnection] = {}

    def get(self, target: str) -> SnowflakeConnection:
        if target not in self._pool:
            conn_config = self.config.connections.snowflake.get(target)
            if not conn_config:
                raise SnowflakeError(f"No Snowflake connection configured for target '{target}'")
            self._pool[target] = SnowflakeConnection(conn_config)
        return self._pool[target]

    def close_all(self):
        for conn in self._pool.values():
            conn.close()
        self._pool.clear()
```

The pool is process-local. SaaS will replace it with a per-tenant pool.

### Read-only mode for agent queries

> **Updated during implementation (2026-04-29):** `is_read_only()` is module-level (not a method) and is the loose, generic classifier. The agent's `run_snowflake_query` tool in M1-04 keeps its own stricter `_is_safe_select` guard that additionally rejects block comments and multi-statement payloads. The two coexist by design.

The `run_snowflake_query` tool from M1-04 needs to enforce read-only:

```python
def is_read_only(sql: str) -> bool:
    """Returns True if the SQL is a SELECT, SHOW, DESCRIBE, or DESC statement."""
    stripped = sql.strip().upper()
    # Strip leading comments
    while stripped.startswith("--") or stripped.startswith("/*"):
        if stripped.startswith("--"):
            newline = stripped.find("\n")
            stripped = stripped[newline + 1:].strip() if newline > 0 else ""
        else:
            close = stripped.find("*/")
            stripped = stripped[close + 2:].strip() if close > 0 else ""

    return any(stripped.startswith(verb) for verb in ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC"))
```

Note `WITH` allows CTEs that lead to a SELECT. We accept some false positives (e.g., `WITH writer AS (...) UPDATE ...`) at the SQL syntactic level — Snowflake will still reject if the user role doesn't have write privileges, and the role we recommend for Carve agents is read-only on most schemas anyway.

For tighter enforcement, parse the SQL with `sqlglot` and inspect the AST. M2 may add this.

### Error formatting

> **Updated during implementation (2026-04-29):** `_format_error` is a module-level helper that returns a `(message, hint, error_code)` tuple. Callers attach the hint and code to a freshly constructed `SnowflakeError` rather than reading them back out of a pre-rendered string. The driver's `errno` is normalized to a 6-digit zero-padded string when looking up hints, and a non-positive/missing `errno` becomes `None`.

When Snowflake returns an error, the message often references SQL line/column. Format errors helpfully:

```python
_ERROR_HINTS: dict[str, str] = {
    "002003": "Object does not exist or access denied. Check that the table/view name is correct and your role has SELECT privileges.",
    "002140": "Schema does not exist or access denied. Check role permissions.",
    "001003": "SQL syntax error. The query failed to parse.",
}

def _format_error(sql: str, exc) -> tuple[str, str | None, str | None]:
    raw = getattr(exc, "errno", None)
    if raw is None or (isinstance(raw, int) and raw <= 0):
        error_code = None
    else:
        error_code = raw if isinstance(raw, str) else f"{raw:06d}"
    hint = _ERROR_HINTS.get(error_code) if error_code else None
    return str(exc), hint, error_code
```

Maintain this hint table; add common ones over time.

### Authentication methods

Three supported in M1:

1. **Password** — set `password = "${SNOWFLAKE_PASSWORD}"` in `connections.toml`
2. **Key-pair** — set `private_key_path = "/path/to/key.p8"`. The class loads and converts the PEM file.
3. **External browser** — set `authenticator = "externalbrowser"`. Pops a browser window for SSO. Useful for dev; not for production.

Key-pair loading helper:

```python
def _load_private_key(self) -> bytes:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    with open(self.config.private_key_path, "rb") as f:
        pem = f.read()

    private_key = serialization.load_pem_private_key(
        pem,
        password=os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "").encode() or None,
        backend=default_backend(),
    )

    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
```

Add `cryptography` to dependencies.

## Tests

Unit tests use mock connections (the `snowflake-connector-python` package supports a fake mode but mocking is simpler for our needs):

- A query returns dict rows
- A query with `limit` adds `LIMIT N` if not present
- `execute()` for INSERT returns row count
- Connection errors are wrapped as `SnowflakeError` with context
- `is_read_only()` correctly classifies SELECT/SHOW/DESCRIBE/WITH/UPDATE/INSERT/DELETE/MERGE/CREATE/DROP

Integration tests (gated on env var presence) hit a real Snowflake account:

- Simple `SELECT 1` works
- `SHOW WAREHOUSES` returns rows
- A 404 (object not found) returns a helpful error

## Acceptance criteria

- The agent's `run_snowflake_query` tool can use this connector
- The Python step's runtime can create connections via the pool
- Three authentication methods all work (manually verified at minimum)
- Errors include hints for common failure modes
- Read-only enforcement blocks write statements via `is_read_only()`

## Files this spec produces

- `src/carve/core/connectors/__init__.py`
- `src/carve/core/connectors/snowflake.py`
- `src/carve/core/connectors/exceptions.py`
- `tests/core/connectors/test_snowflake.py`
- `tests/core/connectors/test_snowflake_integration.py` (gated)

## What this enables

- M1-04's `run_snowflake_query` tool has a working backend
- M1-05's Python steps inherit env vars from the connection config
- M2's `dbt` step type can verify connection health before running
- M3's `sql` step type uses the same connector pool
