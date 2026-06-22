"""Thin wrapper around `snowflake-connector-python`.

Centralizes connection construction, query execution with parameter
binding, and error translation so the rest of the codebase doesn't have
to repeat boilerplate. Three authentication methods are supported in
M1 (per spec): password, key-pair, external browser.

A connection is created lazily on first `connect()` call and cached on
the instance. `SnowflakePool` keys connections by target name so the
same physical session is reused across the agent loop and any other
caller within a single process.

Read-only classification lives here as `is_read_only()` for callers
that want a quick check; M1-04 keeps a stricter `_is_safe_select` for
the agent-tool surface (rejects block comments and multi-statement
payloads). Both coexist by design.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import snowflake.connector
from snowflake.connector import DictCursor
from snowflake.connector.errors import DatabaseError, ProgrammingError

from carve.core.config.schema import Config
from carve.core.config.schema import SnowflakeConnection as ConnConfig
from carve.core.connectors.exceptions import SnowflakeError

if TYPE_CHECKING:
    pass


# Hint table for common Snowflake error codes. Keep extending as we learn
# which failures bite users most often. Codes are strings (not ints) to
# match the zero-padded form Snowflake exposes via `errno`.
_ERROR_HINTS: dict[str, str] = {
    "002003": (
        "Object does not exist or access denied. Check that the table/view "
        "name is correct and your role has SELECT privileges."
    ),
    "002140": "Schema does not exist or access denied. Check role permissions.",
    "001003": "SQL syntax error. The query failed to parse.",
}


def _format_error(sql: str, exc: BaseException) -> tuple[str, str | None, str | None]:
    """Translate a driver exception into (message, hint, error_code).

    `sql` is included for log/debug context but not appended to the
    user-facing message — the driver typically already cites line/column.
    """
    error_code_raw = getattr(exc, "errno", None)
    error_code: str | None
    # The Snowflake driver defaults `errno` to `-1` when no code is set;
    # treat any non-positive int (or missing attr) as "unknown".
    if error_code_raw is None or (isinstance(error_code_raw, int) and error_code_raw <= 0):
        error_code = None
    else:
        # Snowflake codes are 6-digit zero-padded strings like "002003".
        # `errno` may come back as int — normalize.
        error_code = error_code_raw if isinstance(error_code_raw, str) else f"{error_code_raw:06d}"
    hint = _ERROR_HINTS.get(error_code) if error_code else None
    return str(exc), hint, error_code


def is_read_only(sql: str) -> bool:
    """Return True if `sql` looks like a read-only statement.

    Recognized verbs: SELECT, WITH (CTE leading to SELECT), SHOW,
    DESCRIBE, DESC. Leading line and block comments are stripped first.

    This is the loose, generic classifier. The agent's `run_snowflake_query`
    tool uses a stricter `_is_safe_select` guard in `m1_tools.py` that
    additionally rejects block comments and multi-statement payloads.
    """
    stripped = sql.strip().upper()
    while stripped.startswith("--") or stripped.startswith("/*"):
        if stripped.startswith("--"):
            newline = stripped.find("\n")
            stripped = stripped[newline + 1 :].strip() if newline > 0 else ""
        else:
            close = stripped.find("*/")
            stripped = stripped[close + 2 :].strip() if close > 0 else ""
    return any(stripped.startswith(verb) for verb in ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC"))


def _has_limit(sql: str) -> bool:
    """Cheap check for an existing trailing `LIMIT N` clause.

    Scans the last ~32 tokens — a real parser would be ideal but this
    is good enough to avoid double-applying `LIMIT` in `query()`.
    """
    upper = sql.rstrip().rstrip(";").upper()
    # Walk back through whitespace-delimited tokens looking for LIMIT.
    tail = upper.rsplit(None, 8)
    return any(tok == "LIMIT" for tok in tail)


class SnowflakeConnection:
    """Lazy wrapper around a single Snowflake driver connection.

    The driver connection is created on first `connect()` call and
    reused thereafter. `close()` releases it; subsequent calls re-open.
    Designed to satisfy M1-04's `SnowflakeQueryRunner` Protocol via
    `run_query`.
    """

    def __init__(self, config: ConnConfig) -> None:
        self.config = config
        self._connection: Any | None = None

    # -- connection lifecycle ------------------------------------------------

    def connect(self) -> Any:
        """Open (or return the cached) driver connection."""
        if self._connection is not None:
            return self._connection

        kwargs: dict[str, Any] = {
            "account": self.config.account,
            "user": self.config.user,
            "role": self.config.role,
            "warehouse": self.config.warehouse,
            "database": self.config.database,
            "schema": self.config.schema_ or "PUBLIC",
        }

        # Auth precedence: externalbrowser → key-pair → password.
        if self.config.authenticator == "externalbrowser":
            kwargs["authenticator"] = "externalbrowser"
        elif self.config.private_key_path:
            kwargs["private_key"] = self._load_private_key()
        elif self.config.password:
            kwargs["password"] = self.config.password
        else:
            raise SnowflakeError(
                "No authentication method configured.",
                hint=(
                    "Provide `password`, `private_key_path`, or set "
                    '`authenticator = "externalbrowser"` for the connection.'
                ),
            )

        try:
            self._connection = snowflake.connector.connect(**kwargs)
        except DatabaseError as exc:
            _msg, hint, code = _format_error("", exc)
            raise SnowflakeError(
                f"Failed to connect to Snowflake: {exc}",
                hint=hint,
                error_code=code,
            ) from exc
        return self._connection

    def close(self) -> None:
        """Close the underlying driver connection if open."""
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    # -- query / execute -----------------------------------------------------

    def query(
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run a SELECT-style query and return rows as dicts.

        If `limit` is given and the SQL doesn't already have a `LIMIT`,
        one is appended. `params` are passed through as bind parameters
        — callers should NEVER f-string user input into `sql`.
        """
        conn = self.connect()
        cursor = conn.cursor(DictCursor)
        try:
            final_sql = sql
            if limit is not None and not _has_limit(final_sql):
                final_sql = f"{final_sql.rstrip().rstrip(';')} LIMIT {int(limit)}"
            cursor.execute(final_sql, params or {})
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except ProgrammingError as exc:
            message, hint, code = _format_error(sql, exc)
            raise SnowflakeError(message, hint=hint, error_code=code) from exc
        finally:
            cursor.close()

    def execute(
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] | None = None,
    ) -> int:
        """Execute a non-SELECT statement; return rows affected (0 if unknown)."""
        conn = self.connect()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params or {})
            return cursor.rowcount or 0
        except ProgrammingError as exc:
            message, hint, code = _format_error(sql, exc)
            raise SnowflakeError(message, hint=hint, error_code=code) from exc
        finally:
            cursor.close()

    # -- M1-04 SnowflakeQueryRunner Protocol --------------------------------

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
        """Protocol-compliant entry point for the agent's read-only tool.

        Mirrors `query(sql, limit=limit)`. Kept as a separate method to
        match the keyword-only `limit` signature declared in
        `carve.core.agents.m1_tools.SnowflakeQueryRunner`.
        """
        return self.query(sql, limit=limit)

    # -- key-pair helper -----------------------------------------------------

    def _load_private_key(self) -> bytes:
        """Load and convert a PEM key file into PKCS8 DER bytes.

        Reads the optional passphrase from
        `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` (empty/unset means unencrypted).
        """
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        path = self.config.private_key_path
        if not path:
            # Defensive — `connect()` already gates on this branch.
            raise SnowflakeError("private_key_path is not configured.")

        try:
            with open(path, "rb") as f:
                pem = f.read()
        except OSError as exc:
            raise SnowflakeError(
                f"Failed to read private key file at {path!r}: {exc}",
                hint="Check the path and file permissions.",
            ) from exc

        passphrase_env = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
        passphrase: bytes | None = passphrase_env.encode() if passphrase_env else None

        try:
            private_key = serialization.load_pem_private_key(
                pem,
                password=passphrase,
                backend=default_backend(),
            )
        except (ValueError, TypeError) as exc:
            raise SnowflakeError(
                f"Failed to load private key from {path!r}: {exc}",
                hint=(
                    "Ensure the file is a valid PEM private key. If the key is "
                    "encrypted, set SNOWFLAKE_PRIVATE_KEY_PASSPHRASE."
                ),
            ) from exc

        return private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )


class SnowflakePool:
    """Process-local pool of `SnowflakeConnection` instances by target.

    The pool is intentionally simple: one connection per target name,
    created lazily on first `get()`. SaaS will replace this with a
    per-tenant, size-bounded pool.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._pool: dict[str, SnowflakeConnection] = {}

    def get(self, target: str) -> SnowflakeConnection:
        """Return (creating if needed) the connection for `target`.

        Raises `SnowflakeError` if no connection block exists for the
        named target.
        """
        if target in self._pool:
            return self._pool[target]
        conn_config = self.config.connections.snowflake.get(target)
        if conn_config is None:
            available = sorted(self.config.connections.snowflake.keys())
            raise SnowflakeError(
                f"No Snowflake connection configured for target {target!r}.",
                hint=(
                    f"Available targets: {available}. Add a "
                    f"[connections.snowflake.{target}] block to connections.toml."
                ),
            )
        conn = SnowflakeConnection(conn_config)
        self._pool[target] = conn
        return conn

    def close_all(self) -> None:
        """Close every cached connection and clear the pool."""
        for conn in self._pool.values():
            conn.close()
        self._pool.clear()
