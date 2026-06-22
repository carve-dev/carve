"""Unit tests for ``edit`` and ``create_file``.

The spec bar: ``edit`` rejects an edit to a not-read file (i.e. a stale
``old_string``), re-reads at apply time (TOCTOU close), ``create_file``
makes a new file under ``allowed_paths``, an outside-``allowed_paths`` /
symlink-escape write is denied, and ``replace_all`` reports the count.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.tools import ToolExecutionError
from carve.core.agents.tools.fs_tools import make_create_file_tool, make_edit_tool


class TestCreateFile:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        tool = make_create_file_tool(tmp_path)
        result = tool.executor({"path": "a/b.py", "content": "x = 1\n"})
        assert (tmp_path / "a" / "b.py").read_text() == "x = 1\n"
        assert isinstance(result, dict)
        assert result["path"] == "a/b.py"

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        (tmp_path / "exists.py").write_text("old\n")
        tool = make_create_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="already exists"):
            tool.executor({"path": "exists.py", "content": "new\n"})

    def test_allowed_paths_enforced(self, tmp_path: Path) -> None:
        allowed = frozenset({(tmp_path / "el" / "x" / "main.py").resolve()})
        tool = make_create_file_tool(tmp_path, allowed_paths=allowed)
        # On the allow-list → ok.
        assert tool.executor({"path": "el/x/main.py", "content": "ok\n"})
        # Off the allow-list → denied.
        with pytest.raises(ToolExecutionError, match="allow-list"):
            tool.executor({"path": "el/x/other.py", "content": "no\n"})

    def test_outside_project_denied(self, tmp_path: Path) -> None:
        tool = make_create_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="outside the project"):
            tool.executor({"path": "../escape.py", "content": "no\n"})

    def test_on_change_called(self, tmp_path: Path) -> None:
        seen: list[str] = []
        tool = make_create_file_tool(tmp_path, on_change=seen.append)
        tool.executor({"path": "x.py", "content": "y\n"})
        assert seen == ["x.py"]


class TestEdit:
    def test_replaces_unique_match(self, tmp_path: Path) -> None:
        f = tmp_path / "f.py"
        f.write_text("alpha\nbeta\n")
        tool = make_edit_tool(tmp_path)
        result = tool.executor({"path": "f.py", "old_string": "beta", "new_string": "gamma"})
        assert f.read_text() == "alpha\ngamma\n"
        assert isinstance(result, dict)
        assert result["replacements"] == 1

    def test_rejects_edit_to_nonexistent_file(self, tmp_path: Path) -> None:
        tool = make_edit_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match=r"not found|create_file"):
            tool.executor({"path": "ghost.py", "old_string": "a", "new_string": "b"})

    def test_stale_old_string_fails_toctou(self, tmp_path: Path) -> None:
        # Simulate read-at-turn-2 / edit-at-turn-20: the agent's
        # old_string no longer matches the current on-disk bytes (the file
        # changed). edit re-reads at apply and refuses.
        f = tmp_path / "f.py"
        f.write_text("the original line\n")
        tool = make_edit_tool(tmp_path)
        # The file has since changed under the agent's feet:
        f.write_text("a totally different line\n")
        with pytest.raises(ToolExecutionError, match=r"not found|changed"):
            tool.executor(
                {
                    "path": "f.py",
                    "old_string": "the original line",
                    "new_string": "patched",
                }
            )
        # No partial write occurred.
        assert f.read_text() == "a totally different line\n"

    def test_non_unique_match_fails_without_replace_all(self, tmp_path: Path) -> None:
        f = tmp_path / "f.py"
        f.write_text("x\nx\nx\n")
        tool = make_edit_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="matched 3 times"):
            tool.executor({"path": "f.py", "old_string": "x", "new_string": "y"})

    def test_replace_all_reports_count(self, tmp_path: Path) -> None:
        f = tmp_path / "f.py"
        f.write_text("x\nx\nx\n")
        tool = make_edit_tool(tmp_path)
        result = tool.executor(
            {"path": "f.py", "old_string": "x", "new_string": "y", "replace_all": True}
        )
        assert f.read_text() == "y\ny\ny\n"
        assert isinstance(result, dict)
        assert result["replacements"] == 3

    def test_symlink_escape_denied(self, tmp_path: Path) -> None:
        # A symlink inside the project that points outside resolves out of
        # the root → denied at the containment check.
        outside = tmp_path.parent / "outside_target.py"
        outside.write_text("secret\n")
        project = tmp_path / "proj"
        project.mkdir()
        link = project / "link.py"
        try:
            link.symlink_to(outside)
        except OSError:  # pragma: no cover - platform without symlink perms
            pytest.skip("symlinks not permitted on this platform")
        tool = make_edit_tool(project)
        with pytest.raises(ToolExecutionError, match="outside the project"):
            tool.executor({"path": "link.py", "old_string": "secret", "new_string": "leak"})

    def test_allowed_paths_enforced_on_edit(self, tmp_path: Path) -> None:
        target = tmp_path / "el" / "x" / "main.py"
        target.parent.mkdir(parents=True)
        target.write_text("a\n")
        other = tmp_path / "el" / "x" / "other.py"
        other.write_text("a\n")
        allowed = frozenset({target.resolve()})
        tool = make_edit_tool(tmp_path, allowed_paths=allowed)
        assert tool.executor({"path": "el/x/main.py", "old_string": "a", "new_string": "b"})
        with pytest.raises(ToolExecutionError, match="allow-list"):
            tool.executor({"path": "el/x/other.py", "old_string": "a", "new_string": "b"})

    def test_on_change_called_on_success(self, tmp_path: Path) -> None:
        f = tmp_path / "f.py"
        f.write_text("a\n")
        seen: list[str] = []
        tool = make_edit_tool(tmp_path, on_change=seen.append)
        tool.executor({"path": "f.py", "old_string": "a", "new_string": "b"})
        assert seen == ["f.py"]
