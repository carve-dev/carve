"""Unit tests for the three M1 code-agent tools.

These tests intentionally do not mock the Anthropic SDK — they exercise
the tool executors directly. See `test_loop.py` for SDK-level tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from carve.core.agents.m1_tools import (
    _is_safe_select,
    build_m1_tools,
    make_allowlisted_write_file_tool,
    make_read_file_tool,
    make_run_snowflake_query_tool,
    make_write_file_tool,
)
from carve.core.agents.tools import ToolExecutionError


class _StubRunner:
    """Snowflake runner stub: records call args, returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else [{"col": 1}, {"col": 2}]
        self.calls: list[tuple[str, int]] = []

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
        self.calls.append((sql, limit))
        return list(self.rows)


# ----------------------------------------------------------------- read_file


class TestReadFile:
    def test_reads_file_under_root(self, tmp_path: Path) -> None:
        (tmp_path / "hello.txt").write_text("world", encoding="utf-8")
        tool = make_read_file_tool(tmp_path)
        assert tool.executor({"path": "hello.txt"}) == "world"

    def test_reads_nested_file(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "c.txt").write_text("nested", encoding="utf-8")
        tool = make_read_file_tool(tmp_path)
        assert tool.executor({"path": "a/b/c.txt"}) == "nested"

    def test_blocks_path_traversal(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            tool = make_read_file_tool(tmp_path)
            with pytest.raises(ToolExecutionError, match="outside the project"):
                tool.executor({"path": "../outside.txt"})
        finally:
            outside.unlink(missing_ok=True)

    def test_blocks_absolute_path(self, tmp_path: Path) -> None:
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="outside the project"):
            tool.executor({"path": "/etc/passwd"})

    def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="File not found"):
            tool.executor({"path": "nope.txt"})

    def test_empty_path_rejected(self, tmp_path: Path) -> None:
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="non-empty"):
            tool.executor({"path": ""})

    def test_non_string_path_rejected(self, tmp_path: Path) -> None:
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="must be a string"):
            tool.executor({"path": 42})


# ---------------------------------------------------------------- write_file


class TestWriteFile:
    def test_writes_file_under_root(self, tmp_path: Path) -> None:
        tool = make_write_file_tool(tmp_path)
        result = tool.executor({"path": "out.txt", "content": "hi"})
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi"
        assert isinstance(result, dict)
        assert result["path"] == "out.txt"
        assert result["bytes_written"] == 2

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        tool = make_write_file_tool(tmp_path)
        tool.executor({"path": "pipelines/foo/main.py", "content": "x = 1\n"})
        assert (tmp_path / "pipelines" / "foo" / "main.py").exists()

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("old", encoding="utf-8")
        tool = make_write_file_tool(tmp_path)
        tool.executor({"path": "f.txt", "content": "new"})
        assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new"

    def test_blocks_path_traversal(self, tmp_path: Path) -> None:
        tool = make_write_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="outside the project"):
            tool.executor({"path": "../escape.txt", "content": "x"})

    def test_non_string_content_rejected(self, tmp_path: Path) -> None:
        tool = make_write_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="content"):
            tool.executor({"path": "x.txt", "content": 5})


# ------------------------------------------------- write_file (allow-listed)
#
# The allow-listed factory was rehomed here from the retired
# `extract_load_tools.py`; the recovery agent (`recovery/agent.py:444`) is
# the live consumer that binds the `el/<name>/{main.py,requirements.txt}`
# allow-list. These tests prove the defense-in-depth — resolved-path
# containment + allow-list membership — survived the move.


class TestAllowlistedWriteFile:
    def test_accepts_on_list_path(self, tmp_path: Path) -> None:
        allowed = (tmp_path / "el" / "iowa" / "main.py").resolve()
        tool = make_allowlisted_write_file_tool(tmp_path, {allowed})
        result = tool.executor({"path": "el/iowa/main.py", "content": "x = 1\n"})
        assert allowed.read_text(encoding="utf-8") == "x = 1\n"
        assert isinstance(result, dict)
        assert result["path"] == "el/iowa/main.py"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        allowed = (tmp_path / "el" / "iowa" / "requirements.txt").resolve()
        tool = make_allowlisted_write_file_tool(tmp_path, {allowed})
        tool.executor({"path": "el/iowa/requirements.txt", "content": "dlt\n"})
        assert allowed.exists()

    def test_rejects_off_list_path(self, tmp_path: Path) -> None:
        allowed = (tmp_path / "el" / "iowa" / "main.py").resolve()
        tool = make_allowlisted_write_file_tool(tmp_path, {allowed})
        with pytest.raises(ToolExecutionError, match="not on the write allow-list"):
            tool.executor({"path": "el/iowa/secret.py", "content": "x"})
        assert not (tmp_path / "el" / "iowa" / "secret.py").exists()

    def test_blocks_path_traversal(self, tmp_path: Path) -> None:
        allowed = (tmp_path / "el" / "iowa" / "main.py").resolve()
        tool = make_allowlisted_write_file_tool(tmp_path, {allowed})
        with pytest.raises(ToolExecutionError, match="outside the project"):
            tool.executor({"path": "../escape.txt", "content": "x"})

    def test_empty_path_rejected(self, tmp_path: Path) -> None:
        tool = make_allowlisted_write_file_tool(tmp_path, set())
        with pytest.raises(ToolExecutionError, match="non-empty"):
            tool.executor({"path": "", "content": "x"})

    def test_non_string_content_rejected(self, tmp_path: Path) -> None:
        allowed = (tmp_path / "el" / "iowa" / "main.py").resolve()
        tool = make_allowlisted_write_file_tool(tmp_path, {allowed})
        with pytest.raises(ToolExecutionError, match="content"):
            tool.executor({"path": "el/iowa/main.py", "content": 5})


# ------------------------------------------------- extract_load retirement
#
# The M1 hardcoded `extract_load` agent was retired in favor of the
# declarative dlt-engineer on the harness. Its public symbols must no
# longer resolve — this is the "the class disappears" guarantee.


class TestExtractLoadRetirement:
    def test_run_extract_load_agent_import_raises(self) -> None:
        with pytest.raises(ImportError):
            from carve.core.agents import (  # noqa: F401
                run_extract_load_agent,
            )

    @pytest.mark.parametrize(
        "symbol",
        [
            "run_extract_load_agent",
            "ExtractLoadResult",
            "ExtractLoadAgentError",
            "load_extract_load_agent_prompt",
        ],
    )
    def test_el_symbol_gone_from_public_surface(self, symbol: str) -> None:
        import carve.core.agents as agents_pkg

        assert symbol not in agents_pkg.__all__
        assert not hasattr(agents_pkg, symbol)

    def test_extract_load_tools_module_gone(self) -> None:
        with pytest.raises(ImportError):
            import carve.core.agents.tools.extract_load_tools  # noqa: F401


# --------------------------------------------------------- run_snowflake_query


class TestSqlSafetyHelper:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "select * from t",
            "  SHOW DATABASES",
            "DESCRIBE my_table",
            "DESC my_table",
            "WITH cte AS (SELECT 1) SELECT * FROM cte",
            "-- a comment\nSELECT 1",
            "(SELECT 1)",
            # Single trailing semicolon (with whitespace) is still allowed.
            "SELECT 1;",
            "SELECT 1;   \n",
        ],
    )
    def test_allowed(self, sql: str) -> None:
        assert _is_safe_select(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET x = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "CREATE TABLE t (a int)",
            "TRUNCATE t",
            "MERGE INTO t USING s ON ...",
            "",
            "   ",
            "GRANT SELECT ON t TO role",
            # Block comments are not supported in this read-only path.
            "/* block */ SELECT 1",
            # Regression: multi-statement payload must be rejected.
            "SELECT 1; DROP TABLE t",
            # Regression: nested block comments must be rejected.
            "/* /* */ DROP TABLE t */ SELECT 1",
        ],
    )
    def test_rejected(self, sql: str) -> None:
        assert not _is_safe_select(sql)


class TestRunSnowflakeQuery:
    def test_executes_select(self) -> None:
        runner = _StubRunner(rows=[{"x": 1}])
        tool = make_run_snowflake_query_tool(runner)
        result = tool.executor({"sql": "SELECT 1 AS x", "limit": 10})
        assert isinstance(result, dict)
        assert result["row_count"] == 1
        assert result["rows"] == [{"x": 1}]
        assert runner.calls == [("SELECT 1 AS x", 10)]

    def test_default_limit_is_100(self) -> None:
        runner = _StubRunner()
        tool = make_run_snowflake_query_tool(runner)
        tool.executor({"sql": "SELECT 1"})
        assert runner.calls == [("SELECT 1", 100)]

    def test_blocks_insert(self) -> None:
        runner = _StubRunner()
        tool = make_run_snowflake_query_tool(runner)
        with pytest.raises(ToolExecutionError, match="SELECT, SHOW, and DESCRIBE"):
            tool.executor({"sql": "INSERT INTO t VALUES (1)"})
        assert runner.calls == []

    def test_blocks_drop(self) -> None:
        runner = _StubRunner()
        tool = make_run_snowflake_query_tool(runner)
        with pytest.raises(ToolExecutionError, match="SELECT, SHOW, and DESCRIBE"):
            tool.executor({"sql": "DROP TABLE t"})

    def test_blocks_blank_sql(self) -> None:
        runner = _StubRunner()
        tool = make_run_snowflake_query_tool(runner)
        with pytest.raises(ToolExecutionError, match="non-empty"):
            tool.executor({"sql": "   "})

    def test_rejects_non_positive_limit(self) -> None:
        runner = _StubRunner()
        tool = make_run_snowflake_query_tool(runner)
        with pytest.raises(ToolExecutionError, match="positive"):
            tool.executor({"sql": "SELECT 1", "limit": 0})

    def test_rejects_non_integer_limit(self) -> None:
        runner = _StubRunner()
        tool = make_run_snowflake_query_tool(runner)
        with pytest.raises(ToolExecutionError, match="integer"):
            tool.executor({"sql": "SELECT 1", "limit": "100"})


# --------------------------------------------------------- bundle / schemas


class TestBundle:
    def test_build_m1_tools_returns_three(self, tmp_path: Path) -> None:
        runner = _StubRunner()
        tools = build_m1_tools(tmp_path, runner)
        names = [t.name for t in tools]
        assert names == ["read_file", "write_file", "run_snowflake_query"]

    def test_to_schema_shape(self, tmp_path: Path) -> None:
        runner = _StubRunner()
        tools = build_m1_tools(tmp_path, runner)
        schema = tools[0].to_schema()
        assert schema["name"] == "read_file"
        assert "description" in schema
        assert schema["input_schema"]["type"] == "object"
        assert schema["input_schema"]["required"] == ["path"]
