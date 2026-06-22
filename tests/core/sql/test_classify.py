"""sqlglot statement classification — read/write/DDL, fail-closed."""

from __future__ import annotations

import pytest

from carve.core.sql.classify import (
    SqlClassificationError,
    StatementKind,
    classify,
    is_read_only,
)


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT 1", StatementKind.READ),
        ("WITH x AS (SELECT 1) SELECT * FROM x", StatementKind.READ),
        ("SHOW TABLES", StatementKind.READ),
        ("DESCRIBE t", StatementKind.READ),
        ("INSERT INTO t VALUES (1)", StatementKind.WRITE),
        ("UPDATE t SET a = 1", StatementKind.WRITE),
        ("DELETE FROM t", StatementKind.WRITE),
        (
            "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = 1",
            StatementKind.WRITE,
        ),
        ("CREATE TABLE t (a int)", StatementKind.DDL),
        ("ALTER TABLE t ADD COLUMN b int", StatementKind.DDL),
        ("DROP TABLE t", StatementKind.DESTRUCTIVE_DDL),
        ("TRUNCATE TABLE t", StatementKind.DESTRUCTIVE_DDL),
    ],
)
def test_classify(sql: str, kind: StatementKind) -> None:
    assert classify(sql, "snowflake") is kind


def test_cte_that_writes_is_write_not_read() -> None:
    # The exact gap the old regex `is_read_only` missed: a WITH that wraps a
    # write parses as Insert, so it classifies as WRITE.
    sql = "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"
    assert classify(sql, "snowflake") is StatementKind.WRITE
    assert is_read_only(sql, "snowflake") is False


@pytest.mark.parametrize("dialect", ["snowflake", "tsql", "postgres"])
def test_select_into_is_a_write_not_a_read(dialect: str) -> None:
    # SELECT ... INTO materializes a table — it must NOT classify as a read
    # (the gate-bypass the review caught).
    sql = "SELECT * INTO sneaky FROM secrets"
    assert classify(sql, dialect) is StatementKind.WRITE
    assert is_read_only(sql, dialect) is False


def test_trailing_comment_does_not_force_write() -> None:
    # A trailing `;`/comment yields an exp.Semicolon node; it must be dropped,
    # not classified as a write (which would falsely deny the read).
    assert classify("SELECT * FROM t; -- note", "snowflake") is StatementKind.READ
    assert is_read_only("SELECT * FROM t; -- note", "snowflake") is True


def test_comment_only_input_raises() -> None:
    with pytest.raises(SqlClassificationError):
        classify("-- just a comment", "snowflake")


def test_multi_statement_takes_most_privileged() -> None:
    assert classify("SELECT 1; DROP TABLE t", "snowflake") is StatementKind.DESTRUCTIVE_DDL


def test_unparseable_raises_and_is_not_read() -> None:
    with pytest.raises(SqlClassificationError):
        classify("this is (not valid sql", "snowflake")
    assert is_read_only("this is (not valid sql", "snowflake") is False


def test_empty_raises() -> None:
    with pytest.raises(SqlClassificationError):
        classify("   ", "snowflake")


def test_is_read_only_unknown_dialect_fails_closed() -> None:
    assert is_read_only("SELECT 1", "oracle") is False


def test_statement_kind_ordering() -> None:
    # Escalating privilege — used by max() over multi-statement strings.
    assert (
        StatementKind.READ < StatementKind.WRITE < StatementKind.DDL < StatementKind.DESTRUCTIVE_DDL
    )
    assert StatementKind.READ.is_read and not StatementKind.WRITE.is_read
