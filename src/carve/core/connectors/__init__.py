"""Connector layer for external data systems.

For M1, only Snowflake is wired up. The connector module exposes:
* `SnowflakeConnection` — a thin wrapper around the official driver.
* `SnowflakePool` — a process-local cache of connections keyed by target.
* `SnowflakeError` — wrapped errors with hints.
* `is_read_only` — module-level helper used outside the agent's stricter
  guard for callers that just want a quick classification.
"""

from __future__ import annotations

from carve.core.connectors.exceptions import SnowflakeError
from carve.core.connectors.snowflake import (
    SnowflakeConnection,
    SnowflakePool,
    is_read_only,
)

__all__ = [
    "SnowflakeConnection",
    "SnowflakeError",
    "SnowflakePool",
    "is_read_only",
]
