"""The max_mode advisory lint: warns on a dead grant, never blocks the load."""

from __future__ import annotations

import logging
from pathlib import Path

from carve.core.agents.lint import lint_agent_grants
from carve.core.agents.loader import load_agent_file
from carve.core.agents.subagent_registry import SubagentRegistry
from carve.core.config.paths import ProjectPaths


def _capture(logger_name: str, level: int) -> list[logging.LogRecord]:
    """Attach a record-collecting handler to ``logger_name``.

    Robust against other tests mutating global logging state: we attach
    directly to the target logger and force its level, so capture does not
    depend on root-handler propagation.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    logger.addHandler(_Collector())
    logger.setLevel(level)
    # pytest's logging plugin can leave a named logger `disabled=True` after
    # an earlier test in the full run; clear it so our handler receives the
    # record (the warning we assert on is real, the disable flag is test
    # cross-talk, not behavior under test).
    logger.disabled = False
    return records


# `edit` is a write tool (needs `build`); pinned at `read_only` it can never
# fire — a genuinely-unreachable grant.
_UNREACHABLE = """\
---
name: over-granted
description: Grants edit but caps at read_only.
tools: [edit, read_file]
max_mode: read_only
---
prompt
"""

_CLEAN = """\
---
name: fine
description: Grants only reachable tools.
tools: [read_file, grep, bash]
max_mode: read_only
---
prompt
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_unreachable_grant_warns_but_still_loads_and_registers(
    tmp_path: Path,
) -> None:
    agent = load_agent_file(_write(tmp_path, "over.md", _UNREACHABLE))

    # Capture via a handler attached directly to the lint module's logger so
    # the assertion is independent of root-logger / propagation state set by
    # other tests in the full run.
    records = _capture("carve.core.agents.lint", logging.WARNING)
    messages = lint_agent_grants(agent)

    # A warning fired for the dead `edit` grant (both the returned message
    # and a real WARNING log record).
    assert any("edit" in m for m in messages)
    assert any(rec.levelno == logging.WARNING for rec in records)

    # The lint is advisory: the agent still loads AND registers (not blocked,
    # the tool is not dropped).
    registry = SubagentRegistry()
    registry.register_files([agent], root_label="user")
    assert "over-granted" in registry
    spec = registry.resolve("over-granted")
    paths = ProjectPaths.from_root(tmp_path)
    granted = {t.name for t in spec.tool_factory(paths)}
    assert "edit" in granted  # not dropped by the lint


def test_clean_grant_emits_no_warning(tmp_path: Path) -> None:
    agent = load_agent_file(_write(tmp_path, "fine.md", _CLEAN))
    records = _capture("carve.core.agents.lint", logging.WARNING)
    messages = lint_agent_grants(agent)
    assert messages == []
    assert not any(rec.levelno == logging.WARNING for rec in records)
