"""Integration tests for the Snowflake connector.

Skipped unless `CARVE_SNOWFLAKE_INTEGRATION_TEST=1`. Configuration is
read from environment variables so we don't depend on a real
`connections.toml` in CI:

    CARVE_SNOW_ACCOUNT
    CARVE_SNOW_USER
    CARVE_SNOW_PASSWORD       (or CARVE_SNOW_PRIVATE_KEY_PATH, or
                              CARVE_SNOW_AUTHENTICATOR=externalbrowser)
    CARVE_SNOW_ROLE
    CARVE_SNOW_WAREHOUSE
    CARVE_SNOW_DATABASE
    CARVE_SNOW_SCHEMA         (optional; defaults to PUBLIC)

These hit a real warehouse and cost real credits — keep them small.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from carve.core.config.schema import SnowflakeConnection as ConnConfig
from carve.core.connectors import SnowflakeConnection, SnowflakeError

INTEGRATION_FLAG = os.environ.get("CARVE_SNOWFLAKE_INTEGRATION_TEST") == "1"

pytestmark = pytest.mark.skipif(
    not INTEGRATION_FLAG,
    reason="Set CARVE_SNOWFLAKE_INTEGRATION_TEST=1 to enable real-Snowflake tests.",
)


def _conn_from_env() -> ConnConfig:
    """Build a ConnConfig from the CARVE_SNOW_* env vars.

    Raises pytest.skip if required vars are missing — keeps the test
    output friendly when the flag is set but credentials aren't.
    """
    required = {
        "account": os.environ.get("CARVE_SNOW_ACCOUNT"),
        "user": os.environ.get("CARVE_SNOW_USER"),
        "role": os.environ.get("CARVE_SNOW_ROLE"),
        "warehouse": os.environ.get("CARVE_SNOW_WAREHOUSE"),
        "database": os.environ.get("CARVE_SNOW_DATABASE"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        pytest.skip(f"Missing CARVE_SNOW_* env vars: {missing}")

    kwargs: dict[str, object] = {
        "account": required["account"],
        "user": required["user"],
        "role": required["role"],
        "warehouse": required["warehouse"],
        "database": required["database"],
        "schema": os.environ.get("CARVE_SNOW_SCHEMA") or "PUBLIC",
    }
    if pwd := os.environ.get("CARVE_SNOW_PASSWORD"):
        kwargs["password"] = pwd
    if pkp := os.environ.get("CARVE_SNOW_PRIVATE_KEY_PATH"):
        kwargs["private_key_path"] = pkp
    if auth := os.environ.get("CARVE_SNOW_AUTHENTICATOR"):
        kwargs["authenticator"] = auth
    return ConnConfig(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> Iterator[SnowflakeConnection]:
    c = SnowflakeConnection(_conn_from_env())
    try:
        yield c
    finally:
        c.close()


def test_select_one_works(conn: SnowflakeConnection) -> None:
    rows = conn.query("SELECT 1 AS X")
    assert rows == [{"X": 1}]


def test_show_warehouses_returns_rows(conn: SnowflakeConnection) -> None:
    rows = conn.query("SHOW WAREHOUSES", limit=5)
    assert isinstance(rows, list)
    assert len(rows) > 0


def test_object_not_found_returns_helpful_error(conn: SnowflakeConnection) -> None:
    with pytest.raises(SnowflakeError) as exc_info:
        conn.query("SELECT * FROM CARVE_DOES_NOT_EXIST_zzz", limit=1)
    err = exc_info.value
    # The driver returns 002003 for missing objects; assert the hint.
    assert err.error_code == "002003"
    assert err.hint is not None
