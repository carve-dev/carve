"""Classify a SQL statement as read / write / DDL via `sqlglot` — fail-closed.

This replaces the regex read-only guards (`connectors.snowflake.is_read_only`,
`m1_tools._is_safe_select`), which a CTE-that-writes (`WITH x AS (…) INSERT …`)
or a multi-statement string slips past. `sqlglot` parses the real statement
tree, so the classification is grounded in structure, not surface text.

Fail-closed: anything that doesn't parse, or doesn't clearly read, is treated
as a non-read (the `run` gate then requires deploy mode + the write role).
"""

from __future__ import annotations

from enum import IntEnum
from typing import cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from carve.core.sql.dialects import normalize_dialect


class StatementKind(IntEnum):
    """Statement classes, ordered by escalating privilege (low → high).

    Ordering matters: a multi-statement string is classified by its most
    privileged statement (``max``).
    """

    READ = 0  # SELECT / WITH…SELECT / UNION / SHOW / DESCRIBE
    WRITE = 1  # INSERT / UPDATE / DELETE / MERGE (incl. WITH…INSERT)
    DDL = 2  # CREATE / ALTER (non-destructive)
    DESTRUCTIVE_DDL = 3  # DROP / TRUNCATE

    @property
    def is_read(self) -> bool:
        return self is StatementKind.READ

    @property
    def is_write(self) -> bool:
        return self is not StatementKind.READ


class SqlClassificationError(ValueError):
    """SQL could not be parsed/classified — callers treat it as non-read."""


_READ_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Subquery,
    exp.Show,
    exp.Describe,
    exp.Pragma,
)
_WRITE_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
)
_DESTRUCTIVE_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Drop,
    exp.TruncateTable,
)
_DDL_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Create,
    exp.Alter,
)


def _classify_one(statement: exp.Expression) -> StatementKind:
    # Destructive first (a DROP is a kind of DDL but must escalate).
    if isinstance(statement, _DESTRUCTIVE_TYPES):
        return StatementKind.DESTRUCTIVE_DDL
    # `SELECT ... INTO <table>` (and its UNION / parenthesized forms) parses as
    # a Select/Union/Subquery but MATERIALIZES a table — a write. It carries an
    # `exp.Into` node the read-type check below would otherwise wave through, so
    # catch it before the read branch. (An INSERT's INTO is part of the Insert
    # node, not an `exp.Into`, so plain inserts are unaffected.)
    if statement.find(exp.Into) is not None:
        return StatementKind.WRITE
    if isinstance(statement, _WRITE_TYPES):
        return StatementKind.WRITE
    if isinstance(statement, _DDL_TYPES):
        return StatementKind.DDL
    if isinstance(statement, _READ_TYPES):
        return StatementKind.READ
    # Unknown / unparsed command (exp.Command) and anything else: fail closed
    # to WRITE so the run gate requires deploy mode + the write role.
    return StatementKind.WRITE


def classify(sql: str, dialect: str) -> StatementKind:
    """Classify ``sql`` (the most privileged of its statements).

    Raises :class:`SqlClassificationError` when the SQL can't be parsed or is
    empty — callers must treat that as a non-read.
    """
    name = normalize_dialect(dialect)
    try:
        # sqlglot types parse() with its own `Expr` alias; cast to the concrete
        # Expression so the classifier's isinstance dispatch type-checks.
        statements = cast("list[exp.Expression | None]", sqlglot.parse(sql, read=name))
    except SqlglotError as exc:
        raise SqlClassificationError(f"Could not parse SQL: {exc}") from exc
    # Drop None and trailing `;`/comment nodes (sqlglot yields an exp.Semicolon
    # for a trailing comment); they aren't statements and must not classify as
    # a write and falsely deny an otherwise-read query.
    parsed = [s for s in statements if s is not None and not isinstance(s, exp.Semicolon)]
    if not parsed:
        raise SqlClassificationError("No SQL statement found.")
    return max((_classify_one(s) for s in parsed), default=StatementKind.WRITE)


def is_read_only(sql: str, dialect: str = "snowflake") -> bool:
    """Whether ``sql`` is purely a read — fail-closed (False on parse error)."""
    try:
        return classify(sql, dialect).is_read
    except Exception:
        # Fail closed: an unparseable or unknown-dialect string is never a read.
        return False


__all__ = [
    "SqlClassificationError",
    "StatementKind",
    "classify",
    "is_read_only",
]
