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
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from carve.core.agents.recovery.agent import (
    build_tools_for_invocation,
    make_run_snowflake_ddl_tool,
)
from carve.core.agents.recovery.invocation import ElRunInvocation
from carve.core.agents.tools import Tool, ToolExecutionError
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig


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


# ---------------------------------------------------- recovery binds the
# allow-listed write tool (the extract_load retirement rehomed the two-arg
# allow-listed `make_write_file_tool` into `m1_tools`; this proves recovery's
# tool assembly still binds the CONTAINED variant end-to-end, not the plain
# one-arg one — the write scope stays `el/<name>/{main.py,requirements.txt}`).


def _write_tool(tools: list[Tool]) -> Tool:
    return next(t for t in tools if t.name == "write_file")


def test_el_run_invocation_binds_allowlisted_write_tool(tmp_path: Path) -> None:
    """`build_tools_for_invocation` for an EL-run failure binds the allow-listed write."""
    invocation = ElRunInvocation(
        pipeline_name="iowa",
        active_target="dev",
        project_dir=tmp_path,
        config=Config(
            project=ProjectConfig(name="rec-test"),
            models=ModelsConfig(anthropic_api_key="sk-test"),
            server=ServerConfig(state_store="postgresql://stub"),
        ),
        failed_run_id="run_x",
        error_text="boom",
    )
    # The repository is only bound into the read-run-logs tool (never called at
    # construction), so a mock suffices to assemble the tool set offline.
    tools = build_tools_for_invocation(invocation, repository=MagicMock())
    write = _write_tool(tools.tools)

    # On-list write (el/iowa/main.py) is accepted.
    result = write.executor({"path": "el/iowa/main.py", "content": "import dlt\n"})
    assert isinstance(result, dict)
    assert (tmp_path / "el" / "iowa" / "main.py").read_text(encoding="utf-8") == "import dlt\n"

    # An off-list path under the SAME component is rejected — the bound tool is
    # the allow-listed variant, not the plain one-arg writer.
    with pytest.raises(ToolExecutionError, match="not on the write allow-list"):
        write.executor({"path": "el/iowa/secret.py", "content": "x"})
    assert not (tmp_path / "el" / "iowa" / "secret.py").exists()
