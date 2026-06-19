"""The three hardcoded tools for the M1 code agent.

`read_file` and `write_file` operate strictly under a project root.
`run_snowflake_query` only permits read-only statements (SELECT, SHOW,
DESCRIBE / DESC) and is wired through a `SnowflakeQueryRunner`
protocol. The real connector arrives in M1-06 — for now the runner is
injected, which keeps the loop testable without Snowflake credentials.

Path-traversal guard: every path is resolved (with symlinks) and
checked to be a descendant of the project root. Absolute paths and
`..` traversal both fail at this gate.

SQL guard: the first non-comment, non-whitespace token must be one of
SELECT, SHOW, DESCRIBE, DESC, or WITH (the last is allowed because
CTEs that ultimately resolve to SELECT are common). DDL/DML are
rejected with an actionable error.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.agents.tools.secrets_denylist import is_secret_path, secret_path_reason


@runtime_checkable
class SnowflakeQueryRunner(Protocol):
    """Minimal contract the run_snowflake_query tool needs.

    M1-06 will provide a concrete implementation. For M1-04 tests pass
    a stub that records calls and returns canned rows.
    """

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
        """Execute a read-only SQL statement and return rows as dicts."""
        ...


# ---------------------------------------------------------------------------
# Path-traversal guard
# ---------------------------------------------------------------------------


def _resolve_under_root(project_dir: Path, relative: str) -> Path:
    """Resolve `relative` against `project_dir` and assert containment.

    Raises `ToolExecutionError` if the resolved path escapes the root
    via absolute path, `..` traversal, or symlinks.
    """
    if not relative:
        raise ToolExecutionError("Path must be a non-empty string.")

    candidate = (project_dir / relative).resolve()
    root = project_dir.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ToolExecutionError(
            f"Path {relative!r} is outside the project directory and cannot be accessed."
        ) from None
    return candidate


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


READ_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Relative path from the project root.",
        },
    },
    "required": ["path"],
}


def make_read_file_tool(project_dir: Path) -> Tool:
    """Build a `read_file` tool bound to `project_dir`."""

    def _execute(input_: ToolInput) -> ToolResult:
        path = input_.get("path")
        if not isinstance(path, str):
            raise ToolExecutionError("`path` must be a string.")
        target = _resolve_under_root(project_dir, path)
        # Secret-path deny-list: refuse to read credential files in EVERY
        # mode (incl. read_only). Checked on the *resolved* path so a
        # symlink/`..` that lands on `.env` is caught too. This is an
        # additional pre-read check on top of the project-root guard.
        if is_secret_path(target) or is_secret_path(path):
            raise ToolExecutionError(secret_path_reason(path))
        if not target.exists():
            raise ToolExecutionError(f"File not found: {path}")
        if not target.is_file():
            raise ToolExecutionError(f"Path is not a regular file: {path}")
        try:
            return target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"Failed to read {path}: {exc}") from exc

    return Tool(
        name="read_file",
        description="Read the contents of a file in the project directory.",
        input_schema=READ_FILE_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


WRITE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Relative path from the project root.",
        },
        "content": {
            "type": "string",
            "description": "Full file contents to write (UTF-8).",
        },
    },
    "required": ["path", "content"],
}


def make_write_file_tool(project_dir: Path) -> Tool:
    """Build a `write_file` tool bound to `project_dir`."""

    def _execute(input_: ToolInput) -> ToolResult:
        path = input_.get("path")
        content = input_.get("content")
        if not isinstance(path, str):
            raise ToolExecutionError("`path` must be a string.")
        if not isinstance(content, str):
            raise ToolExecutionError("`content` must be a string.")
        target = _resolve_under_root(project_dir, path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"Failed to write {path}: {exc}") from exc
        return {"path": path, "bytes_written": len(content.encode("utf-8"))}

    return Tool(
        name="write_file",
        description=(
            "Write contents to a file in the project directory. Creates parent "
            "directories as needed. Overwrites if the file exists."
        ),
        input_schema=WRITE_FILE_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# run_snowflake_query
# ---------------------------------------------------------------------------


RUN_SNOWFLAKE_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "Read-only SQL to execute."},
        "limit": {
            "type": "integer",
            "default": 100,
            "description": "Maximum rows to return.",
        },
    },
    "required": ["sql"],
}

# Strip leading SQL line comments so the keyword check sees the first
# real token. Block comments (`/* ... */`) are intentionally NOT
# supported in this read-only path: Snowflake permits nested block
# comments, which a single regex pass cannot reliably strip, and any
# input containing `/*` is rejected outright. Line comments (`--`)
# remain allowed.
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_ALLOWED_PREFIXES: tuple[str, ...] = ("SELECT", "SHOW", "DESCRIBE", "DESC", "WITH")


def _is_safe_select(sql: str) -> bool:
    """Return True iff `sql` is a single allowed read-only statement.

    Rejects:
      * any input containing `/*` (block comments are not supported in
        this guard — Snowflake allows nested block comments which a
        simple regex cannot strip safely);
      * multi-statement payloads — an unquoted `;` followed by any
        non-whitespace content is treated as a second statement.

    After those guards, line comments (`--`) are stripped and the first
    remaining token is checked against an allowlist (SELECT, SHOW,
    DESCRIBE, DESC, WITH). `WITH` is allowed because CTEs are a normal
    way to write SELECTs.
    """
    if "/*" in sql:
        return False
    # Multi-statement guard: a `;` is only acceptable as trailing
    # whitespace/comments. Anything non-whitespace after the first `;`
    # means a second statement is present.
    semi = sql.find(";")
    if semi != -1 and sql[semi + 1 :].strip():
        return False
    cleaned = _SQL_LINE_COMMENT_RE.sub(" ", sql)
    stripped = cleaned.lstrip().lstrip("(").lstrip()
    if not stripped:
        return False
    first = stripped.split(None, 1)[0].upper()
    # The first token may have a trailing `;` if the statement is
    # `SELECT;` — strip it before comparison.
    first = first.rstrip(";")
    return first in _ALLOWED_PREFIXES


def make_run_snowflake_query_tool(runner: SnowflakeQueryRunner) -> Tool:
    """Build a `run_snowflake_query` tool that delegates to `runner`."""

    def _execute(input_: ToolInput) -> ToolResult:
        sql = input_.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ToolExecutionError("`sql` must be a non-empty string.")
        limit_raw = input_.get("limit", 100)
        if not isinstance(limit_raw, int) or isinstance(limit_raw, bool):
            raise ToolExecutionError("`limit` must be an integer.")
        if limit_raw <= 0:
            raise ToolExecutionError("`limit` must be a positive integer.")

        if not _is_safe_select(sql):
            raise ToolExecutionError(
                "Only SELECT, SHOW, and DESCRIBE statements are allowed via this tool."
            )

        rows = runner.run_query(sql, limit=limit_raw)
        return {"row_count": len(rows), "rows": rows}

    return Tool(
        name="run_snowflake_query",
        description=(
            "Execute a read-only SQL query against Snowflake. Used for exploring "
            "source data and schemas. Only SELECT, SHOW, and DESCRIBE statements "
            "are allowed."
        ),
        input_schema=RUN_SNOWFLAKE_QUERY_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# submit_plan
# ---------------------------------------------------------------------------


# Schema for the plan agent's terminator tool. Mirrors the design
# document shape from spec M1.1-06's "Plan agent" section. We list every
# named field explicitly so the Anthropic API can validate the model's
# output before it hits us — but `additionalProperties` is left at the
# JSON-Schema default (true) so the agent can include analysis fields
# we haven't enumerated yet without the call being rejected.
SUBMIT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_name": {
            "type": "string",
            "description": (
                "snake_case name for the pipeline. The build agent writes "
                "files to el/<pipeline_name>/."
            ),
        },
        "description": {
            "type": "string",
            "description": "One-line summary of what the pipeline does.",
        },
        "is_new_pipeline": {
            "type": "boolean",
            "description": (
                "True for a brand-new pipeline; False when the design "
                "modifies an existing pipeline (--pipeline <name>)."
            ),
        },
        "source": {
            "type": "object",
            "description": (
                "Where the data comes from. `type` is freeform; common "
                "values: socrata_api, http_csv, snowflake, postgres."
            ),
        },
        "destination": {
            "type": "object",
            "description": (
                "Snowflake destination. `database` and `schema` come from "
                "the active connection target; `table` is named by the "
                "agent (Snowflake convention: UPPER_SNAKE_CASE)."
            ),
            "properties": {
                "database": {"type": "string"},
                "schema": {"type": "string"},
                "table": {"type": "string"},
                "primary_key": {"type": ["string", "null"]},
            },
        },
        "transformation": {
            "type": "object",
            "description": "Strategy choice plus a one-sentence rationale.",
            "properties": {
                "strategy": {
                    "type": "string",
                    "enum": ["truncate_load", "append_only", "merge_upsert"],
                },
                "rationale": {"type": "string"},
            },
        },
        "columns": {
            "type": "array",
            "description": "Canonical column list for the destination table.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "nullable": {"type": "boolean"},
                },
            },
        },
        "requirements": {
            "type": "array",
            "description": (
                "pip-spec strings that go into requirements.txt. Always "
                "includes snowflake-connector-python."
            ),
            "items": {"type": "string"},
        },
        "estimates": {
            "type": "object",
            "description": "Coarse expectations: row count, runtime.",
        },
        "tradeoffs": {
            "type": "array",
            "description": "Known costs / limitations of this design.",
            "items": {"type": "string"},
        },
        "open_questions": {
            "type": "array",
            "description": (
                "Ambiguities the agent surfaces back to the user. Empty "
                "array if the design is fully specified."
            ),
            "items": {"type": "string"},
        },
    },
    "required": [
        "pipeline_name",
        "description",
        "source",
        "destination",
        "transformation",
        "requirements",
    ],
}


class SubmitPlanCapture:
    """Stateful container for the plan agent's `submit_plan` payload.

    The plan agent terminates by calling `submit_plan(...)` exactly
    once. The orchestrator builds a fresh `SubmitPlanCapture`, hands it
    to `make_submit_plan_tool`, runs the loop, and then reads
    ``capture.design`` after the loop returns. If the agent never calls
    `submit_plan` ``capture.design`` is ``None``; callers raise their own
    `PlanGenerationError` in that case.
    """

    def __init__(self) -> None:
        self.design: dict[str, Any] | None = None

    @property
    def submitted(self) -> bool:
        return self.design is not None


def make_submit_plan_tool(capture: SubmitPlanCapture) -> Tool:
    """Build a `submit_plan` tool that records the design on `capture`.

    The tool's executor returns a tiny acknowledgement so the model
    sees the call succeeded; the orchestrator inspects ``capture.design``
    after the loop completes. We do not parse the design with pydantic
    here — the loop's tool layer already validates against the JSON
    schema, and over-strict server-side validation would force a
    cascade of edits whenever the spec's design shape evolves.

    A second invocation within the same `capture` is rejected: the
    plan agent's contract is exactly one design per planning session,
    and the loop's `terminator_tool="submit_plan"` wiring exits after
    the first call. The `called` flag is a belt-and-braces guard for
    the unlikely case the model emits two `submit_plan` blocks in the
    same turn — the first wins and the second surfaces as a tool error.
    """
    # Track whether `submit_plan` has already fired so a second call
    # cannot silently overwrite the captured design.
    called = False

    def _execute(input_: ToolInput) -> ToolResult:
        nonlocal called
        if called:
            raise ToolExecutionError(
                "submit_plan already called; only one design can be "
                "submitted per planning session."
            )
        if not isinstance(input_, dict):
            raise ToolExecutionError("submit_plan input must be an object.")
        # Record a defensive copy so subsequent mutation of the input
        # dict (the loop reuses the parsed payload for logging) can't
        # silently change what the orchestrator sees.
        capture.design = dict(input_)
        called = True
        return {"status": "submitted"}

    return Tool(
        name="submit_plan",
        description=(
            "Finalize the pipeline design. Call this once with the complete "
            "design document. The orchestrator captures the design and the "
            "loop terminates after this call."
        ),
        input_schema=SUBMIT_PLAN_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# Bundle helper
# ---------------------------------------------------------------------------


def build_m1_tools(
    project_dir: Path,
    snowflake_runner: SnowflakeQueryRunner,
) -> list[Tool]:
    """Return the three M1 code-agent tools, bound to project resources.

    Retained for callers that want the legacy combined toolset
    (read_file + write_file + run_snowflake_query). The plan agent and
    build agent each construct narrower toolsets directly.
    """
    return [
        make_read_file_tool(project_dir),
        make_write_file_tool(project_dir),
        make_run_snowflake_query_tool(snowflake_runner),
    ]
