"""Unit tests for ``carve.core.deploy.ddl_applier``.

The applier is exercised against a hand-rolled ``_FakeSnowflake``
instead of the real connector — every test asserts on the parsed
statements and the per-statement execute order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.deploy.ddl_applier import (
    DdlStatementFailure,
    UnsafeDdlError,
    apply_ddl,
    parse_ddl_statements,
    validate_ddl_statements,
)


class _FakeSnowflake:
    """Records executed SQL; can be primed to fail at a given index."""

    def __init__(self, fail_index: int | None = None, error: str = "boom") -> None:
        self.executed: list[str] = []
        self._fail_index = fail_index
        self._error = error

    def execute(
        self, sql: str, params: dict[str, object] | None = None
    ) -> int:
        del params
        self.executed.append(sql)
        if self._fail_index is not None and len(self.executed) - 1 == self._fail_index:
            raise RuntimeError(self._error)
        return 0


# ---------------------------------------------------------------------------
# parse_ddl_statements
# ---------------------------------------------------------------------------


def test_parse_drops_empty_and_comment_only_blocks() -> None:
    sql = """
    -- a comment
    CREATE TABLE IF NOT EXISTS foo (id INT);
    /* block comment */
    ;
    GRANT SELECT ON foo TO ROLE r;
    """
    parsed = parse_ddl_statements(sql)
    assert len(parsed) == 2
    assert "CREATE TABLE" in parsed[0]
    assert "GRANT SELECT" in parsed[1]


def test_parse_strips_trailing_semicolons() -> None:
    sql = "CREATE TABLE IF NOT EXISTS a (id INT);"
    parsed = parse_ddl_statements(sql)
    assert parsed == ["CREATE TABLE IF NOT EXISTS a (id INT)"]


def test_parse_handles_multi_statement_in_order() -> None:
    sql = (
        "CREATE SCHEMA IF NOT EXISTS s;\n"
        "CREATE TABLE IF NOT EXISTS s.t (id INT);\n"
        "GRANT SELECT ON s.t TO ROLE r;\n"
    )
    parsed = parse_ddl_statements(sql)
    assert [s.split()[0] for s in parsed] == ["CREATE", "CREATE", "GRANT"]


# ---------------------------------------------------------------------------
# apply_ddl
# ---------------------------------------------------------------------------


def test_apply_ddl_success(tmp_path: Path) -> None:
    ddl = tmp_path / "x.sql"
    ddl.write_text(
        "CREATE TABLE IF NOT EXISTS a (id INT);\n"
        "CREATE TABLE IF NOT EXISTS b (id INT);\n"
    )
    fake = _FakeSnowflake()
    result = apply_ddl(deploy_connection=fake, ddl_path=ddl)  # type: ignore[arg-type]
    assert result.success
    assert result.statements_executed == 2
    assert len(fake.executed) == 2


def test_apply_ddl_records_failure_index(tmp_path: Path) -> None:
    ddl = tmp_path / "x.sql"
    ddl.write_text(
        "CREATE TABLE IF NOT EXISTS a (id INT);\n"
        "GRANT SELECT ON a TO ROLE r;\n"
        "CREATE TABLE IF NOT EXISTS c (id INT);\n"
    )
    fake = _FakeSnowflake(fail_index=1, error="insufficient privileges")
    result = apply_ddl(deploy_connection=fake, ddl_path=ddl)  # type: ignore[arg-type]
    assert not result.success
    assert result.statements_executed == 1
    assert isinstance(result.failure, DdlStatementFailure)
    assert result.failure.index == 1
    assert "GRANT" in result.failure.sql
    assert "insufficient" in result.failure.error
    # Statement after the failing one must NOT have been executed.
    assert len(fake.executed) == 2  # one success + one failure


def test_apply_ddl_resumes_from_start_index(tmp_path: Path) -> None:
    """Recovery retries skip already-applied statements."""
    ddl = tmp_path / "x.sql"
    ddl.write_text(
        "CREATE TABLE IF NOT EXISTS a (id INT);\n"
        "CREATE TABLE IF NOT EXISTS b (id INT);\n"
        "CREATE TABLE IF NOT EXISTS c (id INT);\n"
    )
    fake = _FakeSnowflake()
    result = apply_ddl(
        deploy_connection=fake,  # type: ignore[arg-type]
        ddl_path=ddl,
        start_index=1,
    )
    assert result.success
    # Only b and c should have been issued.
    assert len(fake.executed) == 2
    assert "CREATE TABLE IF NOT EXISTS b" in fake.executed[0]
    assert "CREATE TABLE IF NOT EXISTS c" in fake.executed[1]


def test_apply_ddl_missing_file(tmp_path: Path) -> None:
    fake = _FakeSnowflake()
    result = apply_ddl(
        deploy_connection=fake,  # type: ignore[arg-type]
        ddl_path=tmp_path / "missing.sql",
    )
    assert not result.success
    assert result.failure is not None
    assert "DDL file not found" in result.failure.error


def test_apply_ddl_start_index_past_end(tmp_path: Path) -> None:
    ddl = tmp_path / "x.sql"
    ddl.write_text("CREATE TABLE IF NOT EXISTS a (id INT);")
    fake = _FakeSnowflake()
    result = apply_ddl(
        deploy_connection=fake,  # type: ignore[arg-type]
        ddl_path=ddl,
        start_index=99,
    )
    assert result.success
    assert result.statements_executed == 0
    assert fake.executed == []


# ---------------------------------------------------------------------------
# DDL allow-list (validate_ddl_statements / UnsafeDdlError)
# ---------------------------------------------------------------------------


def test_ddl_applier_allows_create_if_not_exists() -> None:
    """Positive: the allow-list accepts the contract's idempotent forms."""
    statements = [
        "CREATE TABLE IF NOT EXISTS analytics.raw.iowa (id INT)",
        "CREATE SCHEMA IF NOT EXISTS analytics.raw",
        "CREATE STAGE IF NOT EXISTS analytics.raw.s",
        "CREATE FILE FORMAT IF NOT EXISTS analytics.raw.f TYPE = CSV",
        "GRANT SELECT ON analytics.raw.iowa TO ROLE r",
        "ALTER TABLE analytics.raw.iowa ADD COLUMN IF NOT EXISTS x INT",
        "USE WAREHOUSE wh",
        "USE DATABASE d",
        "USE SCHEMA s",
        "USE ROLE r",
        "COMMENT ON TABLE analytics.raw.iowa IS 'doc'",
        "DROP TABLE IF EXISTS analytics.raw.old",
        "ALTER TABLE analytics.raw.iowa DROP COLUMN IF EXISTS old",
        "DROP SCHEMA IF EXISTS analytics.raw_old RESTRICT",
    ]
    # Should not raise.
    validate_ddl_statements(statements)


def test_ddl_applier_refuses_create_or_replace() -> None:
    with pytest.raises(UnsafeDdlError) as excinfo:
        validate_ddl_statements(
            ["CREATE OR REPLACE TABLE analytics.raw.iowa (id INT)"]
        )
    assert excinfo.value.index == 0
    assert "CREATE OR REPLACE" in excinfo.value.label
    # The error message must NOT contain the SQL text.
    assert "iowa" not in str(excinfo.value)


def test_ddl_applier_refuses_bare_rename() -> None:
    with pytest.raises(UnsafeDdlError) as excinfo:
        validate_ddl_statements(
            ["ALTER TABLE analytics.raw.iowa RENAME TO analytics.raw.bad"]
        )
    assert "RENAME" in excinfo.value.label


def test_ddl_applier_refuses_drop_database() -> None:
    with pytest.raises(UnsafeDdlError) as excinfo:
        validate_ddl_statements(["DROP DATABASE prod"])
    assert "DROP DATABASE" in excinfo.value.label


@pytest.mark.parametrize(
    "stmt",
    [
        "INSERT INTO analytics.raw.iowa VALUES (1)",
        "UPDATE analytics.raw.iowa SET id = 2",
        "DELETE FROM analytics.raw.iowa WHERE id = 1",
        "MERGE INTO analytics.raw.iowa USING src ON id = id WHEN MATCHED THEN UPDATE SET id = src.id",  # noqa: E501
        "TRUNCATE TABLE analytics.raw.iowa",
    ],
)
def test_ddl_applier_refuses_embedded_dml(stmt: str) -> None:
    with pytest.raises(UnsafeDdlError) as excinfo:
        validate_ddl_statements([stmt])
    assert "DML" in excinfo.value.label


def test_ddl_applier_refuses_drop_without_if_exists() -> None:
    with pytest.raises(UnsafeDdlError):
        validate_ddl_statements(["DROP TABLE analytics.raw.iowa"])
    with pytest.raises(UnsafeDdlError):
        validate_ddl_statements(["DROP SCHEMA analytics.raw"])


def test_ddl_applier_refuses_alter_set_data_type() -> None:
    with pytest.raises(UnsafeDdlError) as excinfo:
        validate_ddl_statements(
            [
                "ALTER TABLE analytics.raw.iowa ALTER COLUMN id "
                "SET DATA TYPE BIGINT"
            ]
        )
    assert "SET DATA TYPE" in excinfo.value.label


def test_ddl_applier_aborts_before_executing_any_when_one_forbidden(
    tmp_path: Path,
) -> None:
    """A forbidden statement at the END means we don't run earlier safe ones."""
    ddl = tmp_path / "x.sql"
    ddl.write_text(
        "CREATE TABLE IF NOT EXISTS a (id INT);\n"
        "GRANT SELECT ON a TO ROLE r;\n"
        "DROP DATABASE prod;\n"
    )
    fake = _FakeSnowflake()
    with pytest.raises(UnsafeDdlError) as excinfo:
        apply_ddl(deploy_connection=fake, ddl_path=ddl)  # type: ignore[arg-type]
    assert excinfo.value.index == 2
    # No statements executed — atomicity.
    assert fake.executed == []


def test_ddl_applier_refuses_unrecognized_default_deny() -> None:
    """Default-deny: anything outside the allow-list is forbidden."""
    with pytest.raises(UnsafeDdlError) as excinfo:
        validate_ddl_statements(["VACUUM TABLE analytics.raw.iowa"])
    assert "unrecognized" in excinfo.value.label.lower()
