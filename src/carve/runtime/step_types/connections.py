"""The connection-by-name factory — a new seam for ``sql`` steps.

A ``sql`` step references a ``connection`` by name (``connection = "prod"``).
There is **no** existing ``connection_for(name)`` resolver in the tree:
``ConnectionsConfig`` holds ``snowflake``/``duckdb`` dicts keyed by user-chosen
target names, ``SnowflakePool.get`` resolves Snowflake only, and the DuckDB
connector is constructed directly. This module adds the one missing resolver:
:func:`resolve_connection`, which looks a name up across both blocks and returns
a :class:`ResolvedConnection` — the **live connector** (a ``DuckDBConnection`` or
``SnowflakeConnection``, both exposing ``query``/``execute``) paired with its
**dialect** (the block it appeared under, for SQL classification).

The factory is injectable (the registry-builder threads it through to the sql
executor) and **DuckDB-default** for tests: a name under
``[connections.duckdb.*]`` yields a creds-free in-process connector, so the
whole sql path runs offline.

Name collision to mind: ``core/config/schema.SnowflakeConnection`` is the *config
model*; ``core/connectors/snowflake.SnowflakeConnection`` is the *live
connector*. This factory takes the former and constructs the latter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from carve.core.config.schema import ConnectionsConfig


class Connection(Protocol):
    """The minimal live-connector surface a ``sql`` step needs.

    Both shipped connectors (``DuckDBConnection``, ``SnowflakeConnection``)
    satisfy this: ``run_query`` returns capped dict rows, ``execute`` runs a
    write/DDL, and ``close`` releases the session. The sql executor depends only
    on this surface, never on a concrete connector type. The dialect is carried
    on :class:`ResolvedConnection` rather than the connector (the Snowflake
    connector exposes no ``dialect`` attribute).
    """

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]: ...

    def execute(self, sql: str) -> Any: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ResolvedConnection:
    """A live connector paired with its dialect.

    ``dialect`` is the block the connection name appeared under
    (``duckdb``/``snowflake``); the sql executor uses it to classify a
    statement as a read (capture rows) vs a write/DDL.

    A context manager: ``with resolve_connection(...) as resolved:`` owns the
    connector for the duration of one step and closes it on exit, so the
    executor never leaks a session (a real cost for Snowflake). ``close`` is
    best-effort — a connector that fails to close still releases the ``with``.
    """

    connection: Connection
    dialect: str

    def __enter__(self) -> ResolvedConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connector, swallowing close-time errors."""
        try:
            self.connection.close()
        except Exception:
            # close is best-effort cleanup — a connector that fails to close
            # must not mask the step's real result or break the `with`.
            pass


class ConnectionResolutionError(Exception):
    """Raised when a ``sql`` step's ``connection`` name doesn't resolve."""


def resolve_connection(name: str, config: ConnectionsConfig) -> ResolvedConnection:
    """Resolve a connection ``name`` to a live connector + its dialect.

    Looks ``name`` up under ``[connections.duckdb.*]`` first (the creds-free
    default), then ``[connections.snowflake.*]``; the dialect is whichever block
    the name appears under. Raises :class:`ConnectionResolutionError` when the
    name keys neither block.
    """
    duckdb_block = config.duckdb.get(name)
    if duckdb_block is not None:
        from carve.core.connectors.duckdb import DIALECT, DuckDBConnection

        return ResolvedConnection(DuckDBConnection(database=duckdb_block.path), DIALECT)

    snowflake_block = config.snowflake.get(name)
    if snowflake_block is not None:
        from carve.core.connectors.snowflake import SnowflakeConnection

        return ResolvedConnection(SnowflakeConnection(snowflake_block), "snowflake")

    available = sorted({*config.duckdb, *config.snowflake})
    raise ConnectionResolutionError(
        f"No connection named {name!r}. Available connections: {available or '(none configured)'}."
    )


# The injected connection-factory seam: name + config → a resolved connection.
# The default is :func:`resolve_connection`; the registry-builder threads an
# override through for tests (DuckDB-default keeps the sql path creds-free).
ConnectionFactory = Callable[[str, "ConnectionsConfig"], ResolvedConnection]


__all__ = [
    "Connection",
    "ConnectionFactory",
    "ConnectionResolutionError",
    "ResolvedConnection",
    "resolve_connection",
]
