"""State-store configuration schema.

The state store is a Postgres database; the SQLite default that M1 shipped
with is gone. This module defines the pydantic shape the config loader
parses, and provides a single helper that picks the *effective* URL from
the merged config — honoring the env-var-friendly ``${DATABASE_URL}``
form used by the docker-compose bundle in v0.1-02.

The legacy ``server.state_store`` key remains as a write-only alias —
the loader still validates it, but the runtime ignores it in favor of
``state_store.url`` once a project has been upgraded. See
`docs/upgrade-from-walking-skeleton.md` for the migration story.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from carve.core.config.schema import Config

# The docker-compose bundle in v0.1-02 ships exactly this connection
# string; the OSS default matches so a fresh install + `carve serve`
# Just Works.
#
# **Dev-only.** These credentials (`carve` / `carve` against a localhost
# Postgres) are intended for local development only. They MUST NOT be
# used for any internet-reachable install. Production deployments must
# override via the ``DATABASE_URL`` env var or by setting
# ``state_store.url`` in ``runtime.toml`` to a connection string whose
# credentials you actually control. The hosted product never uses this
# default — its control plane injects a managed connection string at
# startup.
DEFAULT_STATE_STORE_URL = "postgresql+psycopg://carve:carve@localhost:5432/carve"


class StateStoreConfig(BaseModel):
    """``[state_store]`` section of ``runtime.toml`` / ``server.toml``.

    Pool sizing defaults (10 / 20) suit the v0.1 default of a single
    worker; the v0.1-07 runtime spec revisits this when concrete worker
    counts land. ``${DATABASE_URL}`` interpolation is applied by the
    loader before this model sees the value, so by validation time
    ``url`` is a concrete connection string.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = DEFAULT_STATE_STORE_URL
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=20, ge=0, le=200)


def resolve_state_store_url(config: Config) -> str:
    """Resolve the effective state-store URL from a loaded config.

    Precedence (highest to lowest):
    1. ``state_store.url`` from ``runtime.toml`` — an explicit, non-default
       value wins over any env var.
    2. ``DATABASE_URL`` env var — the canonical Postgres env var. Honored
       even when no ``runtime.toml`` has been written yet (the
       ``carve init`` bootstrap case).
    3. ``server.state_store`` from the legacy ``server.toml`` — kept for
       M1 in-tree projects that haven't migrated yet. Removed in v0.2.
    4. The module default (``DEFAULT_STATE_STORE_URL``).
    """
    if config.state_store.url != DEFAULT_STATE_STORE_URL:
        return config.state_store.url
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    if config.server.state_store and config.server.state_store != DEFAULT_STATE_STORE_URL:
        return config.server.state_store
    return config.state_store.url
