"""Apply a Snowflake DDL file via the deploy role (Phase 6).

The DDL file is parsed with ``sqlparse``, comment-only and empty
statements are filtered, and the remainder are executed in source
order against the deploy role's connection. On any failure, the
applier surfaces:

* The failing statement's index (0-based) so a recovery handler can
  retry from there without re-executing already-applied DDL within a
  single deploy attempt.
* The full SQL text of the failing statement.
* The Snowflake driver's error.

The contract:

* DDL must be **idempotent** (`CREATE OR REPLACE`, `IF NOT EXISTS`,
  `GRANT IF EXISTS`). Re-running after a partial failure is safe per
  the P1-06 contract.
* Statement *order* is preserved — we never reorder. The recovery
  handler is the only thing allowed to edit the file and re-run.
* Multi-statement fixtures with semicolons are split via ``sqlparse``;
  a single trailing semicolon is normalized off so the connector
  doesn't see an empty statement.
* Before executing **any** statement, every parsed statement is
  classified against an allow-list. A single forbidden statement
  short-circuits the apply and raises :class:`UnsafeDdlError` — the
  recovery agent does NOT participate in this failure mode (the
  error is structural and re-prompting the agent is dangerous).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import sqlparse

if TYPE_CHECKING:
    from carve.core.connectors.snowflake import SnowflakeConnection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Statement classification (allow-list)
# ---------------------------------------------------------------------------


# We classify against the normalized *leading* tokens of each statement.
# sqlparse's `Statement.get_type()` is unreliable for `CREATE OR REPLACE`
# vs `CREATE ... IF NOT EXISTS` discrimination, so we use anchored
# regexes against the prefix instead.
_HEAD_LEN = 80


def _normalize_head(stmt: str) -> str:
    """Return the case-insensitive normalized leading bytes of ``stmt``.

    Comments and whitespace runs are collapsed; the result is the
    first ``_HEAD_LEN`` characters of the cleaned form, uppercased
    so the allow/forbid regexes can be plain ASCII.
    """
    # Strip comments first — sqlparse handles both -- and /* */ forms.
    body = sqlparse.format(stmt, strip_comments=True).strip()
    # Collapse runs of whitespace so "ALTER\nTABLE\n... DROP\nCOLUMN"
    # and "ALTER TABLE ... DROP COLUMN" classify identically.
    body = re.sub(r"\s+", " ", body)
    return body[:_HEAD_LEN].upper()


# Allow-list patterns. Each must match against the normalized head.
# These cover the idempotent DDL families P1-06's contract permits.
# `\b` boundaries keep `CREATE TABLE` from matching `CREATE TABLEAU`.
_ALLOW_PATTERNS: tuple[re.Pattern[str], ...] = (
    # CREATE ... IF NOT EXISTS family
    re.compile(
        r"^CREATE\s+(?:OR\s+ALTER\s+)?"
        r"(?:TABLE|VIEW|SCHEMA|STAGE|FILE\s+FORMAT|DATABASE|WAREHOUSE)\s+"
        r"IF\s+NOT\s+EXISTS\b"
    ),
    # GRANT statements
    re.compile(r"^GRANT\b"),
    # ALTER TABLE ... ADD COLUMN IF NOT EXISTS (idempotent column add)
    re.compile(
        r"^ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\b"
    ),
    # USE statements (set context; harmless and sometimes emitted at
    # the head of a DDL file)
    re.compile(r"^USE\s+(?:WAREHOUSE|DATABASE|SCHEMA|ROLE)\b"),
    # COMMENT ON ... IS '...' (metadata-only)
    re.compile(r"^COMMENT\s+ON\b"),
    # DROP TABLE IF EXISTS — safe explicit cleanup
    re.compile(r"^DROP\s+TABLE\s+IF\s+EXISTS\b"),
    # ALTER TABLE ... DROP COLUMN IF EXISTS — safe explicit cleanup
    re.compile(
        r"^ALTER\s+TABLE\s+\S+\s+DROP\s+COLUMN\s+IF\s+EXISTS\b"
    ),
    # DROP SCHEMA IF EXISTS <name> RESTRICT
    re.compile(r"^DROP\s+SCHEMA\s+IF\s+EXISTS\s+\S+\s+RESTRICT\b"),
)

# Forbid-list patterns. These trigger BEFORE the allow check (so a
# CREATE OR REPLACE TABLE doesn't sneak through against a too-loose
# allow rule). The forbid set names what's known-dangerous; the
# allow set is the positive contract.
_FORBID_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^CREATE\s+OR\s+REPLACE\b"), "CREATE OR REPLACE"),
    (
        re.compile(r"^ALTER\s+TABLE\s+\S+\s+RENAME\b"),
        "ALTER TABLE ... RENAME",
    ),
    (
        re.compile(
            r"^ALTER\s+TABLE\s+\S+\s+ALTER\s+COLUMN\b.*SET\s+DATA\s+TYPE\b"
        ),
        "ALTER COLUMN SET DATA TYPE",
    ),
    (re.compile(r"^INSERT\b"), "INSERT (DML)"),
    (re.compile(r"^UPDATE\b"), "UPDATE (DML)"),
    (re.compile(r"^DELETE\b"), "DELETE (DML)"),
    (re.compile(r"^MERGE\b"), "MERGE (DML)"),
    (re.compile(r"^TRUNCATE\b"), "TRUNCATE (DML)"),
    (re.compile(r"^DROP\s+DATABASE\b"), "DROP DATABASE"),
)


class UnsafeDdlError(Exception):
    """Raised when a DDL file contains a forbidden statement.

    Carries the offending statement's index and a human-readable
    label of which rule it violated. The full SQL text is intentionally
    *not* on the exception to keep it out of persisted error messages
    (a malicious or buggy DDL file might embed credentials).

    Recovery does not participate in this failure mode: the file is
    structurally unsafe and re-prompting the agent is more dangerous
    than aborting.
    """

    def __init__(self, *, index: int, label: str) -> None:
        self.index = index
        self.label = label
        super().__init__(
            f"DDL statement #{index} is forbidden ({label}). "
            "Recovery does not participate in this failure mode; "
            "fix the DDL file by hand and re-run."
        )


def _classify_statement(stmt: str) -> str | None:
    """Return ``None`` if allowed, or a label describing why it's forbidden.

    Forbid rules are checked first; an explicit forbid wins even if
    an allow rule could also match. Anything that matches no allow
    rule is forbidden by default ("unrecognized DDL").
    """
    head = _normalize_head(stmt)
    for pattern, label in _FORBID_PATTERNS:
        if pattern.match(head):
            return label
    for pattern in _ALLOW_PATTERNS:
        if pattern.match(head):
            return None
    return "unrecognized DDL"


def validate_ddl_statements(parsed_statements: list[str]) -> None:
    """Raise :class:`UnsafeDdlError` if any statement is forbidden.

    Validates **all** statements before any are executed. A single
    forbidden statement aborts the whole file — the deploy command
    surfaces the index and label, never the SQL itself.
    """
    for index, stmt in enumerate(parsed_statements):
        label = _classify_statement(stmt)
        if label is not None:
            raise UnsafeDdlError(index=index, label=label)


@dataclass
class DdlStatementFailure:
    """Capture the failure point for a recovery handoff."""

    index: int
    sql: str
    error: str


@dataclass
class DdlApplyResult:
    """Outcome of `apply_ddl`. ``failure is None`` means success."""

    statements_executed: int = 0
    failure: DdlStatementFailure | None = None
    parsed_statements: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.failure is None


def parse_ddl_statements(sql_text: str) -> list[str]:
    """Split ``sql_text`` into executable statements.

    Empty / comment-only blocks are dropped. Trailing semicolons on
    each statement are stripped — Snowflake's connector accepts them
    but tests assert on the parsed list directly.
    """
    raw = sqlparse.parse(sql_text)
    out: list[str] = []
    for stmt in raw:
        # `Statement.tokens` includes comment / whitespace tokens; the
        # easiest filter is to look at the rendered form.
        rendered = str(stmt).strip()
        if not rendered:
            continue
        # Drop everything after stripping comments to test for an
        # empty body. ``sqlparse.format`` returns the SQL minus
        # comments — if that's empty (or just punctuation), the
        # statement was comments-only.
        body = sqlparse.format(rendered, strip_comments=True).strip()
        # Strip stray semicolons: `/* x */ ;` -> `;` after comment
        # stripping; that's still effectively empty.
        if not body.strip(";").strip():
            continue
        # Trim a single trailing semicolon. Don't trim multiple — a
        # trailing ";;" means an empty statement which the parser
        # would already have filtered.
        if rendered.endswith(";"):
            rendered = rendered[:-1].rstrip()
        if rendered:
            out.append(rendered)
    return out


def apply_ddl(
    *,
    deploy_connection: SnowflakeConnection,
    ddl_path: Path,
    start_index: int = 0,
) -> DdlApplyResult:
    """Execute every parsed statement in ``ddl_path`` against the deploy role.

    ``start_index`` lets a recovery retry skip statements that already
    landed within the same deploy. ``DdlApplyResult.statements_executed``
    is the count of successfully applied statements *in this call*
    (resumes after a recovery skip earlier statements).

    The applier surfaces per-statement failures via
    ``DdlApplyResult.failure``. Statement *classification* failures
    (an explicit forbidden form, an unrecognized DDL family) raise
    :class:`UnsafeDdlError` instead — those are structural and don't
    participate in recovery.
    """
    if not ddl_path.is_file():
        return DdlApplyResult(
            failure=DdlStatementFailure(
                index=0,
                sql="",
                error=f"DDL file not found: {ddl_path}",
            ),
        )

    sql_text = ddl_path.read_text(encoding="utf-8")
    statements = parse_ddl_statements(sql_text)

    # Validate the FULL parsed statement list before executing anything.
    # Atomicity: a forbidden statement at the end of the file means we
    # don't execute the safe ones at the head. Better to surface the
    # whole file as unsafe than to land partial state.
    validate_ddl_statements(statements)

    result = DdlApplyResult(parsed_statements=list(statements))

    if start_index < 0:
        start_index = 0
    if start_index >= len(statements):
        # Nothing to do — already past the end of the file. Treat as
        # success; this happens when a recovery resumes at exactly
        # the count of statements (the file was truncated).
        return result

    for index, statement in enumerate(statements):
        if index < start_index:
            continue
        try:
            deploy_connection.execute(statement)
            result.statements_executed += 1
        except Exception as exc:
            result.failure = DdlStatementFailure(
                index=index,
                sql=statement,
                error=str(exc),
            )
            return result

    return result


__all__ = [
    "DdlApplyResult",
    "DdlStatementFailure",
    "UnsafeDdlError",
    "apply_ddl",
    "parse_ddl_statements",
    "validate_ddl_statements",
]
