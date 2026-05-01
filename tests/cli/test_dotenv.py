"""Tests for the inline ``.env`` parser used by the CLI auto-loader.

The parser is intentionally small: anything beyond the ``KEY=value`` /
``KEY="value"`` / ``KEY='value'`` shape is silently skipped, and existing
``os.environ`` values are never clobbered unless ``override=True``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from carve.cli.dotenv import load_dotenv

_TOUCHED_KEYS = (
    "FOO",
    "BAR",
    "QUOTED",
    "SINGLE",
    "BARE",
    "EMPTY",
    "ESCAPED",
    "CAFE",
    "TRUNC",
    "OTHER",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Snapshot any prior values, clear, then restore on teardown.

    Both setup and teardown are needed: tests in this module set values
    on ``os.environ`` directly via ``load_dotenv``, so without an explicit
    teardown those values would leak into the rest of the test session.
    """
    snapshot: dict[str, str | None] = {
        key: os.environ.get(key) for key in _TOUCHED_KEYS
    }
    for key in _TOUCHED_KEYS:
        monkeypatch.delenv(key, raising=False)
    try:
        yield
    finally:
        for key, prior in snapshot.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


def test_load_dotenv_sets_unset_keys(tmp_path: Path, clean_env: None) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\n")

    set_keys = load_dotenv(env_file)

    assert set_keys == {"FOO": "bar"}
    assert os.environ["FOO"] == "bar"


def test_load_dotenv_does_not_override_by_default(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOO", "shell")
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=file\n")

    set_keys = load_dotenv(env_file)

    assert set_keys == {}
    assert os.environ["FOO"] == "shell"


def test_load_dotenv_override_flag(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOO", "shell")
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=file\n")

    set_keys = load_dotenv(env_file, override=True)

    assert set_keys == {"FOO": "file"}
    assert os.environ["FOO"] == "file"


def test_load_dotenv_handles_quotes_and_comments(
    tmp_path: Path, clean_env: None
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n"
        "# a leading comment line\n"
        "\n"
        'QUOTED="quoted value"\n'
        "SINGLE='single'\n"
        "BARE=bare\n"
        "EMPTY= # trailing comment\n"
    )

    set_keys = load_dotenv(env_file)

    assert set_keys == {
        "QUOTED": "quoted value",
        "SINGLE": "single",
        "BARE": "bare",
        "EMPTY": "",
    }
    assert os.environ["QUOTED"] == "quoted value"
    assert os.environ["SINGLE"] == "single"
    assert os.environ["BARE"] == "bare"
    assert os.environ["EMPTY"] == ""


def test_load_dotenv_missing_file_is_noop(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.env"
    assert load_dotenv(missing) == {}


def test_load_dotenv_skips_malformed_lines(tmp_path: Path, clean_env: None) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "not_a_kv_line\n"
        "=missing_key\n"
        "FOO=bar\n"
        "  also-not-a-kv  \n"
        "BAR=baz\n"
    )

    set_keys = load_dotenv(env_file)

    # Malformed lines silently skipped; valid lines still load.
    assert set_keys == {"FOO": "bar", "BAR": "baz"}
    assert os.environ["FOO"] == "bar"
    assert os.environ["BAR"] == "baz"


def test_load_dotenv_handles_backslash_escapes_in_double_quotes(
    tmp_path: Path, clean_env: None
) -> None:
    env_file = tmp_path / ".env"
    # Literal two-char `\n` sequence in the source — the loader turns it
    # into an actual newline at parse time.
    env_file.write_text('ESCAPED="line1\\nline2"\n')

    set_keys = load_dotenv(env_file)

    assert set_keys == {"ESCAPED": "line1\nline2"}
    assert os.environ["ESCAPED"] == "line1\nline2"


def test_load_dotenv_truncated_escape_does_not_crash(
    tmp_path: Path, clean_env: None
) -> None:
    """An unsupported escape like ``\\x`` is kept literal — never raises.

    The previous implementation routed double-quoted bodies through
    ``bytes.decode("unicode_escape")``, which raised ``UnicodeDecodeError``
    on truncated ``\\x`` sequences and aborted the whole CLI. The new
    table-driven decoder treats unknown escapes as a literal backslash
    plus the next char, so other valid keys still load.
    """
    env_file = tmp_path / ".env"
    env_file.write_text('TRUNC="\\x"\nOTHER=ok\n')

    set_keys = load_dotenv(env_file)

    # `\x` is not in the escape table → kept as the two literal chars.
    assert set_keys == {"TRUNC": "\\x", "OTHER": "ok"}
    assert os.environ["TRUNC"] == "\\x"
    assert os.environ["OTHER"] == "ok"


def test_load_dotenv_non_ascii_in_double_quotes_is_literal(
    tmp_path: Path, clean_env: None
) -> None:
    """Non-ASCII inside double quotes round-trips as UTF-8, not Latin-1.

    The old ``unicode_escape`` path mojibake'd these characters by
    re-encoding the UTF-8 string as Latin-1. The table-driven decoder
    leaves them untouched.
    """
    env_file = tmp_path / ".env"
    env_file.write_text('CAFE="café"\n', encoding="utf-8")

    set_keys = load_dotenv(env_file)

    assert set_keys == {"CAFE": "café"}
    assert os.environ["CAFE"] == "café"


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX chmod semantics — Windows treats 0o000 differently",
)
def test_load_dotenv_unreadable_file_returns_empty(
    tmp_path: Path, clean_env: None
) -> None:
    """A file that exists but can't be read is silently treated as empty.

    The 'missing file is not an error' contract extends to 'unreadable
    file is also not an error' — the CLI must not blow up on a permission
    glitch in the middle of the user's project dir.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\n")
    env_file.chmod(0o000)
    try:
        # Some CI environments (notably running as root) ignore mode bits
        # and can still read the file. If so, the assertion below would
        # be testing the wrong thing — skip rather than false-positive.
        try:
            with env_file.open("rb"):
                pytest.skip("running as root or filesystem ignores chmod")
        except OSError:
            pass

        set_keys = load_dotenv(env_file)
        assert set_keys == {}
        assert "FOO" not in os.environ
    finally:
        # Restore mode so tmp_path cleanup can remove the file.
        env_file.chmod(0o600)
