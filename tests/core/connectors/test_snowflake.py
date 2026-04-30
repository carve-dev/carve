"""Unit tests for the Snowflake connector layer.

These tests mock at `snowflake.connector.connect` so no real network
traffic happens. Real-warehouse coverage lives in
`test_snowflake_integration.py` and is gated on an env var.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from snowflake.connector.errors import DatabaseError, ProgrammingError

from carve.core.agents.m1_tools import SnowflakeQueryRunner
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
)
from carve.core.config.schema import (
    SnowflakeConnection as ConnConfig,
)
from carve.core.connectors import SnowflakeConnection, SnowflakeError, SnowflakePool, is_read_only
from carve.core.connectors.snowflake import _format_error, _has_limit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn_config(**overrides: Any) -> ConnConfig:
    base: dict[str, Any] = {
        "account": "acct.region",
        "user": "carve_user",
        "password": "secret",
        "role": "CARVE_RW",
        "warehouse": "CARVE_WH",
        "database": "CARVE_DB",
        "schema": "PUBLIC",
    }
    base.update(overrides)
    return ConnConfig(**base)


def _make_config(targets: dict[str, ConnConfig] | None = None) -> Config:
    return Config(
        project=ProjectConfig(name="t"),
        connections=ConnectionsConfig(snowflake=targets or {}),
        models=ModelsConfig(anthropic_api_key="x"),
    )


def _mock_driver_connection(
    *,
    fetchall_rows: list[Any] | None = None,
    rowcount: int = 0,
    raise_on_execute: BaseException | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like a Snowflake driver connection."""
    cursor = MagicMock()
    if raise_on_execute is not None:
        cursor.execute.side_effect = raise_on_execute
    cursor.fetchall.return_value = fetchall_rows or []
    cursor.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


# ---------------------------------------------------------------------------
# is_read_only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT 1", True),
        ("  select * from t", True),
        ("WITH x AS (SELECT 1) SELECT * FROM x", True),
        ("SHOW TABLES", True),
        ("DESCRIBE TABLE foo", True),
        ("DESC TABLE foo", True),
        ("-- comment\nSELECT 1", True),
        ("/* hi */ SELECT 1", True),
        ("UPDATE t SET x = 1", False),
        ("INSERT INTO t VALUES (1)", False),
        ("DELETE FROM t", False),
        ("MERGE INTO t USING s ON t.id = s.id", False),
        ("CREATE TABLE t (x INT)", False),
        ("DROP TABLE t", False),
        ("", False),
    ],
)
def test_is_read_only(sql: str, expected: bool) -> None:
    assert is_read_only(sql) is expected


# ---------------------------------------------------------------------------
# _has_limit
# ---------------------------------------------------------------------------


def test_has_limit_detects_existing_limit() -> None:
    assert _has_limit("SELECT * FROM t LIMIT 10")
    assert _has_limit("SELECT * FROM t LIMIT 10;")
    assert not _has_limit("SELECT * FROM t")
    assert not _has_limit("SELECT * FROM t WHERE x = 1")


# ---------------------------------------------------------------------------
# _format_error
# ---------------------------------------------------------------------------


def test_format_error_attaches_hint_for_known_code() -> None:
    exc = ProgrammingError("Object X does not exist")
    exc.errno = 2003
    msg, hint, code = _format_error("SELECT * FROM x", exc)
    assert "does not exist" in msg
    assert hint is not None and "Check that the table" in hint
    assert code == "002003"


def test_format_error_no_hint_for_unknown_code() -> None:
    exc = ProgrammingError("Some other failure")
    exc.errno = 999999
    _msg, hint, code = _format_error("SELECT 1", exc)
    assert hint is None
    assert code == "999999"


def test_format_error_handles_missing_errno() -> None:
    exc = ProgrammingError("No errno here")
    _msg, hint, code = _format_error("SELECT 1", exc)
    assert hint is None
    assert code is None


# ---------------------------------------------------------------------------
# SnowflakeError
# ---------------------------------------------------------------------------


def test_snowflake_error_renders_hint() -> None:
    err = SnowflakeError("oops", hint="try this")
    assert "oops" in str(err)
    assert "Hint: try this" in str(err)


def test_snowflake_error_without_hint() -> None:
    err = SnowflakeError("oops")
    assert str(err) == "oops"


# ---------------------------------------------------------------------------
# SnowflakeConnection.connect — auth precedence
# ---------------------------------------------------------------------------


@patch("snowflake.connector.connect")
def test_connect_uses_password_auth(mock_connect: MagicMock) -> None:
    mock_connect.return_value = _mock_driver_connection()
    cfg = _conn_config(password="hunter2")
    conn = SnowflakeConnection(cfg)
    conn.connect()
    kwargs = mock_connect.call_args.kwargs
    assert kwargs["password"] == "hunter2"
    assert "authenticator" not in kwargs
    assert "private_key" not in kwargs


@patch("snowflake.connector.connect")
def test_connect_uses_external_browser(mock_connect: MagicMock) -> None:
    mock_connect.return_value = _mock_driver_connection()
    cfg = _conn_config(password=None, authenticator="externalbrowser")
    conn = SnowflakeConnection(cfg)
    conn.connect()
    assert mock_connect.call_args.kwargs["authenticator"] == "externalbrowser"


@patch("snowflake.connector.connect")
def test_connect_key_pair_loads_pem(
    mock_connect: MagicMock,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key-pair auth: write a real key, expect DER bytes passed through."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    keyfile = tmp_path / "rsa.p8"
    keyfile.write_bytes(pem)
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", raising=False)

    mock_connect.return_value = _mock_driver_connection()
    cfg = _conn_config(password=None, private_key_path=str(keyfile))
    conn = SnowflakeConnection(cfg)
    conn.connect()
    pk = mock_connect.call_args.kwargs["private_key"]
    assert isinstance(pk, bytes) and len(pk) > 0


def test_connect_raises_when_no_auth_configured() -> None:
    cfg = _conn_config(password=None)
    conn = SnowflakeConnection(cfg)
    with pytest.raises(SnowflakeError, match="No authentication method"):
        conn.connect()


@patch("snowflake.connector.connect")
def test_connect_wraps_database_error(mock_connect: MagicMock) -> None:
    err = DatabaseError("auth failed")
    err.errno = 250001
    mock_connect.side_effect = err
    conn = SnowflakeConnection(_conn_config())
    with pytest.raises(SnowflakeError, match="Failed to connect"):
        conn.connect()


@patch("snowflake.connector.connect")
def test_connect_caches_connection(mock_connect: MagicMock) -> None:
    mock_connect.return_value = _mock_driver_connection()
    conn = SnowflakeConnection(_conn_config())
    a = conn.connect()
    b = conn.connect()
    assert a is b
    assert mock_connect.call_count == 1


@patch("snowflake.connector.connect")
def test_connect_default_schema_is_public(mock_connect: MagicMock) -> None:
    mock_connect.return_value = _mock_driver_connection()
    cfg = _conn_config(schema=None)
    conn = SnowflakeConnection(cfg)
    conn.connect()
    assert mock_connect.call_args.kwargs["schema"] == "PUBLIC"


# ---------------------------------------------------------------------------
# query / execute
# ---------------------------------------------------------------------------


@patch("snowflake.connector.connect")
def test_query_returns_dict_rows(mock_connect: MagicMock) -> None:
    rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    mock_connect.return_value = _mock_driver_connection(fetchall_rows=rows)
    conn = SnowflakeConnection(_conn_config())
    out = conn.query("SELECT id, name FROM t")
    assert out == rows


@patch("snowflake.connector.connect")
def test_query_appends_limit_when_missing(mock_connect: MagicMock) -> None:
    driver = _mock_driver_connection(fetchall_rows=[])
    mock_connect.return_value = driver
    conn = SnowflakeConnection(_conn_config())
    conn.query("SELECT * FROM t", limit=50)
    sql_arg = driver.cursor.return_value.execute.call_args.args[0]
    assert sql_arg.endswith("LIMIT 50")


@patch("snowflake.connector.connect")
def test_query_does_not_double_apply_limit(mock_connect: MagicMock) -> None:
    driver = _mock_driver_connection(fetchall_rows=[])
    mock_connect.return_value = driver
    conn = SnowflakeConnection(_conn_config())
    conn.query("SELECT * FROM t LIMIT 5", limit=50)
    sql_arg = driver.cursor.return_value.execute.call_args.args[0]
    assert sql_arg.upper().count("LIMIT") == 1


@patch("snowflake.connector.connect")
def test_query_passes_bind_parameters(mock_connect: MagicMock) -> None:
    driver = _mock_driver_connection(fetchall_rows=[])
    mock_connect.return_value = driver
    conn = SnowflakeConnection(_conn_config())
    conn.query("SELECT * FROM t WHERE id = %(id)s", params={"id": 7})
    args, _kwargs = driver.cursor.return_value.execute.call_args
    assert args[1] == {"id": 7}


@patch("snowflake.connector.connect")
def test_query_wraps_programming_error_with_hint(mock_connect: MagicMock) -> None:
    err = ProgrammingError("Object FOO does not exist")
    err.errno = 2003
    mock_connect.return_value = _mock_driver_connection(raise_on_execute=err)
    conn = SnowflakeConnection(_conn_config())
    with pytest.raises(SnowflakeError) as exc_info:
        conn.query("SELECT * FROM foo")
    assert exc_info.value.error_code == "002003"
    assert exc_info.value.hint is not None
    assert "Check that the table" in exc_info.value.hint


@patch("snowflake.connector.connect")
def test_execute_returns_rowcount(mock_connect: MagicMock) -> None:
    mock_connect.return_value = _mock_driver_connection(rowcount=3)
    conn = SnowflakeConnection(_conn_config())
    n = conn.execute("INSERT INTO t VALUES (1), (2), (3)")
    assert n == 3


@patch("snowflake.connector.connect")
def test_execute_returns_zero_when_rowcount_is_none(mock_connect: MagicMock) -> None:
    driver = _mock_driver_connection()
    driver.cursor.return_value.rowcount = None
    mock_connect.return_value = driver
    conn = SnowflakeConnection(_conn_config())
    assert conn.execute("CREATE TABLE t (x INT)") == 0


@patch("snowflake.connector.connect")
def test_execute_wraps_programming_error(mock_connect: MagicMock) -> None:
    err = ProgrammingError("syntax error")
    err.errno = 1003
    mock_connect.return_value = _mock_driver_connection(raise_on_execute=err)
    conn = SnowflakeConnection(_conn_config())
    with pytest.raises(SnowflakeError) as exc_info:
        conn.execute("INSRT INTO t VALUES (1)")
    assert exc_info.value.error_code == "001003"


# ---------------------------------------------------------------------------
# run_query (M1-04 Protocol surface)
# ---------------------------------------------------------------------------


@patch("snowflake.connector.connect")
def test_run_query_satisfies_runner_protocol(mock_connect: MagicMock) -> None:
    rows = [{"a": 1}]
    mock_connect.return_value = _mock_driver_connection(fetchall_rows=rows)
    conn = SnowflakeConnection(_conn_config())
    # Structural Protocol check.
    assert isinstance(conn, SnowflakeQueryRunner)
    out = conn.run_query("SELECT 1", limit=10)
    assert out == rows


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@patch("snowflake.connector.connect")
def test_close_releases_driver_connection(mock_connect: MagicMock) -> None:
    driver = _mock_driver_connection()
    mock_connect.return_value = driver
    conn = SnowflakeConnection(_conn_config())
    conn.connect()
    conn.close()
    driver.close.assert_called_once()
    # Re-open works.
    conn.connect()
    assert mock_connect.call_count == 2


def test_close_is_noop_when_never_connected() -> None:
    conn = SnowflakeConnection(_conn_config())
    conn.close()  # should not raise


# ---------------------------------------------------------------------------
# SnowflakePool
# ---------------------------------------------------------------------------


def test_pool_returns_same_instance_per_target() -> None:
    cfg = _make_config({"dev": _conn_config(), "prod": _conn_config(role="PROD_RW")})
    pool = SnowflakePool(cfg)
    a = pool.get("dev")
    b = pool.get("dev")
    assert a is b
    c = pool.get("prod")
    assert c is not a


def test_pool_unknown_target_raises_with_hint() -> None:
    cfg = _make_config({"dev": _conn_config()})
    pool = SnowflakePool(cfg)
    with pytest.raises(SnowflakeError) as exc_info:
        pool.get("staging")
    assert "staging" in str(exc_info.value)
    assert "dev" in str(exc_info.value)  # hint lists available targets


@patch("snowflake.connector.connect")
def test_pool_close_all_closes_each(mock_connect: MagicMock) -> None:
    mock_connect.return_value = _mock_driver_connection()
    cfg = _make_config({"dev": _conn_config(), "prod": _conn_config()})
    pool = SnowflakePool(cfg)
    pool.get("dev").connect()
    pool.get("prod").connect()
    pool.close_all()
    assert pool._pool == {}
