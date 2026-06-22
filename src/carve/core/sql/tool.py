"""The dialect-aware `sql` tool every agent can call.

Ops: ``validate`` / ``transpile`` / ``introspect`` / ``run``. The dialect is
fixed at construction (resolved from the connection); so is the
:class:`PermissionMode` and the read/write runners — the harness gate admits the
tool by *name* only and never passes the mode to the executor, so mode-aware
read/write/DDL enforcement is baked in here (mirroring how the orchestrator
rebuilds gated tools per child mode).

Enforcement (the shipped `warehouse_roles` floor): reads run on the read runner
in any mode; writes/DDL require **deploy** mode (``role_for`` raises below it)
and the write runner; destructive DDL (DROP/TRUNCATE) additionally needs
approval (denied headless). ``generate`` / ``modify`` / ``explain`` are the SQL
specialist's LLM job, not tool ops.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from sqlglot.errors import SqlglotError

from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.warehouse_roles import WarehouseWriteDenied, role_for
from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.sql.classify import SqlClassificationError, StatementKind, classify
from carve.core.sql.dialects import UnsupportedDialectError, transpile, validate
from carve.core.sql.introspect import (
    INTROSPECT_OPS,
    InvalidIdentifierError,
    UnsupportedIntrospectionError,
)

DEFAULT_ROW_CAP = 100


class ReadRunner(Protocol):
    """Read surface: capped query for `run`, raw query for `introspect`."""

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]: ...
    def query(
        self, sql: str, params: dict[str, Any] | list[Any] | None = None
    ) -> list[dict[str, Any]]: ...


class WriteRunner(Protocol):
    """Write surface used only for deploy-mode writes/DDL."""

    def execute(self, sql: str) -> None: ...


_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["validate", "transpile", "introspect", "run"],
            "description": "The SQL operation to perform.",
        },
        "sql": {"type": "string", "description": "SQL for validate / transpile / run."},
        "from_dialect": {"type": "string", "description": "transpile: source dialect."},
        "to_dialect": {"type": "string", "description": "transpile: target dialect."},
        "kind": {
            "type": "string",
            "enum": list(INTROSPECT_OPS),
            "description": "introspect: which catalog read.",
        },
        "database": {"type": "string"},
        "schema": {"type": "string"},
        "table": {"type": "string"},
        "include_views": {"type": "boolean"},
    },
    "required": ["op"],
}

_INTROSPECT_KEYS = ("database", "schema", "table", "include_views")


def make_sql_tool(
    *,
    dialect: str,
    mode: PermissionMode,
    read_runner: ReadRunner,
    write_runner: WriteRunner | None = None,
    approver: Callable[[str], bool] | None = None,
    name: str = "sql",
    row_cap: int = DEFAULT_ROW_CAP,
) -> Tool:
    """Build a `sql` tool bound to one connection's dialect + the active mode."""

    def _execute(input_: ToolInput) -> ToolResult:
        op = input_.get("op")
        if op == "validate":
            result = validate(_require_sql(input_), dialect)
            return {"ok": result.ok, "error": result.error}
        if op == "transpile":
            return _transpile(input_)
        if op == "introspect":
            return _introspect(input_)
        if op == "run":
            return _run(input_)
        raise ToolExecutionError(f"Unknown sql op {op!r}; use validate/transpile/introspect/run.")

    def _transpile(input_: ToolInput) -> ToolResult:
        try:
            out = transpile(
                _require_sql(input_),
                read=input_.get("from_dialect") or dialect,
                write=input_.get("to_dialect") or dialect,
            )
        except (UnsupportedDialectError, SqlglotError) as exc:
            raise ToolExecutionError(f"transpile failed: {exc}") from exc
        return {"sql": out}

    def _introspect(input_: ToolInput) -> ToolResult:
        kind = input_.get("kind")
        fn = INTROSPECT_OPS.get(kind) if isinstance(kind, str) else None
        if fn is None:
            raise ToolExecutionError(
                f"introspect needs a 'kind' in {list(INTROSPECT_OPS)}; got {kind!r}."
            )
        kwargs = {k: input_[k] for k in _INTROSPECT_KEYS if k in input_ and input_[k] is not None}
        try:
            return fn(read_runner, dialect, **kwargs)
        except TypeError as exc:  # missing/extra arg for this kind
            raise ToolExecutionError(f"introspect {kind}: bad arguments ({exc}).") from exc
        except (InvalidIdentifierError, UnsupportedIntrospectionError) as exc:
            raise ToolExecutionError(str(exc)) from exc

    def _run(input_: ToolInput) -> ToolResult:
        sql = _require_sql(input_)
        try:
            kind = classify(sql, dialect)
        except SqlClassificationError as exc:
            raise ToolExecutionError(f"refusing to run unclassifiable SQL: {exc}") from exc

        if kind.is_read:
            # Over-fetch one row so an exact-cap result isn't reported truncated.
            fetched = read_runner.run_query(sql, limit=row_cap + 1)
            truncated = len(fetched) > row_cap
            rows = fetched[:row_cap]
            return {"rows": rows, "row_count": len(rows), "truncated": truncated}

        # Write / DDL: deploy-mode + write role only (fail-closed below deploy).
        try:
            role_for(mode=mode, is_write=True)
        except WarehouseWriteDenied as exc:
            raise ToolExecutionError(str(exc)) from exc
        if write_runner is None:
            raise ToolExecutionError("No write runner configured for this connection.")
        if kind is StatementKind.DESTRUCTIVE_DDL and not (approver and approver(_ddl_prompt(sql))):
            raise ToolExecutionError("Destructive DDL (DROP/TRUNCATE) requires approval; denied.")
        write_runner.execute(sql)
        return {"executed": True, "kind": kind.name.lower()}

    return Tool(
        name=name,
        description=(
            "Dialect-aware SQL: validate (parse-check), transpile (between "
            "dialects), introspect (read the real schema), run (execute, "
            "permission-gated — writes/DDL only in deploy mode). The dialect is "
            "the connection's; author dialect-correct SQL and validate before running."
        ),
        input_schema=_INPUT_SCHEMA,
        executor=_execute,
    )


def _require_sql(input_: ToolInput) -> str:
    sql = input_.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ToolExecutionError("This sql op requires a non-empty 'sql' string.")
    return sql


def _ddl_prompt(sql: str) -> str:
    return f"Approve destructive DDL?\n{sql.strip()[:500]}"


__all__ = ["DEFAULT_ROW_CAP", "ReadRunner", "WriteRunner", "make_sql_tool"]
