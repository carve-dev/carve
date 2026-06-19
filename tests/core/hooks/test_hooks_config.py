"""Hook config parse tests (hooks.toml → HookSpec)."""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.hooks.config import (
    HookConfigError,
    load_hooks_config,
    parse_hooks_config,
)
from carve.core.hooks.events import HookEvent

_TOML = """\
[[hook]]
on = "pre_tool"
match = { tool = "bash", command = "git commit*" }
run = "sqlfluff lint {changed_sql}"

[[hook]]
on = "on_run_failed"
run = "notify-slack {pipeline}"
"""


def test_parses_entries(tmp_path: Path) -> None:
    path = tmp_path / "hooks.toml"
    path.write_text(_TOML, encoding="utf-8")
    specs = load_hooks_config(path)
    assert len(specs) == 2
    assert specs[0].event is HookEvent.PRE_TOOL
    assert specs[0].match.tool == "bash"
    assert specs[0].match.command == "git commit*"
    # A deferred-emitter event parses (subscription seam), no firing here.
    assert specs[1].event is HookEvent.ON_RUN_FAILED


def test_missing_file_is_no_hooks(tmp_path: Path) -> None:
    assert load_hooks_config(tmp_path / "absent.toml") == []


def test_unknown_event_raises() -> None:
    with pytest.raises(HookConfigError, match="unknown event"):
        parse_hooks_config({"hook": [{"on": "pre_lunch", "run": "x"}]})


def test_missing_run_raises() -> None:
    with pytest.raises(HookConfigError, match="`run`"):
        parse_hooks_config({"hook": [{"on": "pre_tool"}]})
