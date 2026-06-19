"""Unit tests for the secret-path deny-list on read tools (every mode).

The spec bar: ``read_file('.env')`` and ``grep`` over ``**/secrets.toml``
are denied in **all** modes — including ``read_only`` — so a read-only
explorer can't leak a credential file into an answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.m1_tools import make_read_file_tool
from carve.core.agents.tools import ToolExecutionError
from carve.core.agents.tools.search_tools import make_glob_tool, make_grep_tool
from carve.core.agents.tools.secrets_denylist import is_secret_path


class TestIsSecretPath:
    @pytest.mark.parametrize(
        "name",
        [".env", ".env.local", ".env.production", "secrets.toml", "key.pem"],
    )
    def test_matches_secret_names(self, name: str) -> None:
        assert is_secret_path(name)

    @pytest.mark.parametrize(
        "name",
        [
            # Case variants — same file on a case-insensitive FS.
            ".ENV",
            ".Env",
            ".eNv.LOCAL",
            "SECRETS.TOML",
            "Secrets.Toml",
            "KEY.PEM",
            "key.PEM",
            # Trailing-dot / trailing-space variants — same file on an FS
            # that normalizes them (Windows).
            ".env ",
            ".env.",
            ".env. ",
            "secrets.toml ",
            "key.pem.",
            ".ENV ",
        ],
    )
    def test_matches_case_and_trailing_variants(self, name: str) -> None:
        assert is_secret_path(name)

    @pytest.mark.parametrize("name", ["main.py", "config.toml", "README.md"])
    def test_allows_ordinary_names(self, name: str) -> None:
        assert not is_secret_path(name)

    def test_matches_nested_secrets_toml(self) -> None:
        assert is_secret_path("/proj/.dlt/secrets.toml")


class TestReadFileDeniesSecrets:
    def test_read_file_env_denied(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-secret\n")
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match=r"credentials|not allowed"):
            tool.executor({"path": ".env"})

    def test_read_file_secrets_toml_denied(self, tmp_path: Path) -> None:
        sub = tmp_path / ".dlt"
        sub.mkdir()
        (sub / "secrets.toml").write_text("token = 'abc'\n")
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError):
            tool.executor({"path": ".dlt/secrets.toml"})

    def test_read_file_pem_denied(self, tmp_path: Path) -> None:
        (tmp_path / "id.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError):
            tool.executor({"path": "id.pem"})

    def test_read_file_uppercase_env_denied(self, tmp_path: Path) -> None:
        # `.ENV` opens the same file as `.env` on a case-insensitive FS;
        # the deny-list must catch it regardless. Plant via the requested
        # name so the test is correct on case-sensitive filesystems too.
        (tmp_path / ".ENV").write_text("SECRET=leaked_via_case\n")
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match=r"credentials|not allowed"):
            tool.executor({"path": ".ENV"})

    def test_read_file_trailing_dot_env_denied(self, tmp_path: Path) -> None:
        # A trailing-dot/space variant must be denied *before* any read —
        # so it is denied even when no such file exists on disk.
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match=r"credentials|not allowed"):
            tool.executor({"path": ".env."})
        with pytest.raises(ToolExecutionError, match=r"credentials|not allowed"):
            tool.executor({"path": ".env "})

    def test_read_file_ordinary_file_allowed(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("print('hi')\n")
        tool = make_read_file_tool(tmp_path)
        assert tool.executor({"path": "main.py"}) == "print('hi')\n"


class TestGrepGlobSkipSecrets:
    def test_grep_never_reads_secrets_toml(self, tmp_path: Path) -> None:
        (tmp_path / "secrets.toml").write_text("password = 'super-secret'\n")
        (tmp_path / "code.py").write_text("password = 'in-code'\n")
        tool = make_grep_tool(tmp_path)
        result = tool.executor({"pattern": "password"})
        assert isinstance(result, dict)
        paths = {m["path"] for m in result["matches"]}
        assert "secrets.toml" not in paths
        assert "code.py" in paths
        # The secret value must never appear in any returned line.
        for match in result["matches"]:
            assert "super-secret" not in match["text"]

    def test_glob_never_returns_env(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("X=1\n")
        (tmp_path / "main.py").write_text("x = 1\n")
        tool = make_glob_tool(tmp_path)
        result = tool.executor({"pattern": "*"})
        assert isinstance(result, dict)
        assert ".env" not in result["matches"]
        assert "main.py" in result["matches"]
