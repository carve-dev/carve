"""Unit tests for the recovery-agent tool factories (P1-09 iter1 fixes).

The recovery agent gets a `run_snowflake_ddl` tool in the DDL-apply
trigger context. P1-08 ships an allow-list (`validate_ddl_statements`)
that gates file-driven DDL; the agent's tool MUST honor the same list
or the agent could bypass safety rules with a single tool call.

These tests assert that:

* destructive DDL families (DROP DATABASE, CREATE OR REPLACE, DML) are
  rejected before the executor sees them; and
* idempotent CREATE TABLE IF NOT EXISTS still flows through.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from carve.core.agents.recovery.agent import make_run_snowflake_ddl_tool
from carve.core.agents.tools import ToolExecutionError


@dataclass
class _RecordingExecutor:
    """Fake executor satisfying the `_DdlExecutor` Protocol.

    Records every SQL it runs and returns 0 (rows affected). Used by
    tests to assert whether the tool reached the executor or rejected
    the SQL up front.
    """

    calls: list[str] = field(default_factory=list)

    def execute(self, sql: str) -> int:
        self.calls.append(sql)
        return 0


def test_run_snowflake_ddl_rejects_drop_database() -> None:
    """`DROP DATABASE prod` is rejected by the allow-list before execute."""
    executor = _RecordingExecutor()
    tool = make_run_snowflake_ddl_tool(executor)

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.executor({"sql": "DROP DATABASE prod"})

    assert "DDL rejected by allow-list" in str(excinfo.value)
    assert executor.calls == []


def test_run_snowflake_ddl_rejects_dml_insert() -> None:
    """`INSERT` is DML, not DDL — must be rejected by the allow-list."""
    executor = _RecordingExecutor()
    tool = make_run_snowflake_ddl_tool(executor)

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.executor({"sql": "INSERT INTO foo (a) VALUES (1)"})

    assert "DDL rejected by allow-list" in str(excinfo.value)
    assert executor.calls == []


def test_run_snowflake_ddl_rejects_create_or_replace() -> None:
    """`CREATE OR REPLACE` drops underlying data — rejected."""
    executor = _RecordingExecutor()
    tool = make_run_snowflake_ddl_tool(executor)

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.executor({"sql": "CREATE OR REPLACE TABLE foo (a INT)"})

    assert "DDL rejected by allow-list" in str(excinfo.value)
    assert executor.calls == []


def test_run_snowflake_ddl_accepts_idempotent_create() -> None:
    """`CREATE TABLE IF NOT EXISTS` flows through to the executor."""
    executor = _RecordingExecutor()
    tool = make_run_snowflake_ddl_tool(executor)

    result = tool.executor({"sql": "CREATE TABLE IF NOT EXISTS foo (a INT)"})

    assert result == {"status": "ok"}
    assert len(executor.calls) == 1
    assert "CREATE TABLE IF NOT EXISTS foo" in executor.calls[0]


def test_run_snowflake_ddl_rejects_multi_statement() -> None:
    """Multi-statement input still rejected before allow-list check."""
    executor = _RecordingExecutor()
    tool = make_run_snowflake_ddl_tool(executor)

    with pytest.raises(ToolExecutionError) as excinfo:
        tool.executor(
            {
                "sql": (
                    "CREATE TABLE IF NOT EXISTS foo (a INT); "
                    "CREATE TABLE IF NOT EXISTS bar (b INT);"
                )
            }
        )

    assert "Multi-statement input is not allowed" in str(excinfo.value)
    assert executor.calls == []
