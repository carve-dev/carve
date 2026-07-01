"""Unit tests: token discovery order + the secret never appears in output."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from carve.mcp.auth import MCPAuthError, resolve_token


def test_cli_token_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CARVE_API_TOKEN", "env-token")
    token_file = tmp_path / "token"
    token_file.write_text("file-token", encoding="utf-8")
    assert resolve_token("cli-token", token_path=token_file) == "cli-token"


def test_env_var_beats_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CARVE_API_TOKEN", "env-token")
    token_file = tmp_path / "token"
    token_file.write_text("file-token", encoding="utf-8")
    assert resolve_token(None, token_path=token_file) == "env-token"


def test_file_used_when_no_cli_or_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CARVE_API_TOKEN", raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("  file-token\n", encoding="utf-8")  # trimmed
    assert resolve_token(None, token_path=token_file) == "file-token"


def test_missing_everything_raises_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CARVE_API_TOKEN", raising=False)
    with pytest.raises(MCPAuthError) as excinfo:
        resolve_token(None, token_path=tmp_path / "does-not-exist")
    message = str(excinfo.value)
    assert "No Carve API token found" in message
    assert "carve auth" in message


def test_empty_file_is_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CARVE_API_TOKEN", raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("   \n", encoding="utf-8")
    with pytest.raises(MCPAuthError):
        resolve_token(None, token_path=token_file)


def test_error_message_never_contains_a_token_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "super-secret-token-value"
    monkeypatch.delenv("CARVE_API_TOKEN", raising=False)
    with caplog.at_level(logging.DEBUG):
        # A successful resolution must not log the secret anywhere.
        resolved = resolve_token(secret, token_path=tmp_path / "nope")
    assert resolved == secret
    assert secret not in caplog.text
