"""Registry override + dispatch-time hot-reload (mtime-cache) tests."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from carve.core.agents.discovery import AgentDiscovery


def _capture(logger_name: str, level: int) -> list[logging.LogRecord]:
    """Attach a record-collecting handler directly to ``logger_name``.

    Independent of root-handler propagation state (other tests in the full
    suite mutate global logging), so the override-log assertion is stable.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    logger.addHandler(_Collector())
    logger.setLevel(level)
    # See the lint test's _capture: clear a stale pytest `disabled=True`.
    logger.disabled = False
    return records

_BUILTIN = """\
---
name: dlt-engineer
description: Built-in dlt engineer.
max_mode: build
classifications: [new_pipeline]
---
Built-in prompt v1.
"""

_USER_OVERRIDE = """\
---
name: dlt-engineer
description: User dlt engineer (override).
max_mode: build
classifications: [new_pipeline]
---
User override prompt.
"""


def _write(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_user_file_overrides_builtin_and_logs(tmp_path: Path) -> None:
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    _write(builtin_dir, "dlt-engineer.md", _BUILTIN)
    _write(user_dir, "dlt-engineer.md", _USER_OVERRIDE)

    discovery = AgentDiscovery.for_project(
        agents_dir=user_dir, builtin_dir=builtin_dir
    )
    records = _capture("carve.core.agents.subagent_registry", logging.INFO)
    registry = discovery.build_registry()

    spec = registry.resolve("dlt-engineer")
    assert spec.system_prompt == "User override prompt."
    assert any(
        "overrides an earlier registration" in rec.getMessage()
        for rec in records
    )


def test_hot_reload_picks_up_change_at_dispatch(tmp_path: Path) -> None:
    """A changed file is re-read at build_registry (dispatch), not before."""
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    agent_path = _write(user_dir, "dlt-engineer.md", _USER_OVERRIDE)
    builtin_dir.mkdir(parents=True, exist_ok=True)

    discovery = AgentDiscovery.for_project(
        agents_dir=user_dir, builtin_dir=builtin_dir
    )

    first = discovery.build_registry().resolve("dlt-engineer")
    assert first.system_prompt == "User override prompt."

    # Mutate the file + bump its mtime so the cache invalidates.
    changed = _USER_OVERRIDE.replace(
        "User override prompt.", "User override prompt v2."
    )
    agent_path.write_text(changed, encoding="utf-8")
    _bump_mtime(agent_path)

    # The change is observed at the NEXT dispatch (build_registry), which is
    # exactly the hot-reload-at-dispatch contract.
    second = discovery.build_registry().resolve("dlt-engineer")
    assert second.system_prompt == "User override prompt v2."


def test_unchanged_file_uses_cache(tmp_path: Path) -> None:
    """An unchanged file (same mtime) returns the cached parse."""
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    _write(user_dir, "dlt-engineer.md", _USER_OVERRIDE)

    discovery = AgentDiscovery.for_project(
        agents_dir=user_dir, builtin_dir=builtin_dir
    )
    first = discovery.build_registry().resolve("dlt-engineer")
    # Second build with no file change reads from the cache and is identical.
    second = discovery.build_registry().resolve("dlt-engineer")
    assert first.system_prompt == second.system_prompt == "User override prompt."


def test_duplicate_name_within_a_root_raises_through_build_registry(
    tmp_path: Path,
) -> None:
    """Two .md in one dir declaring the same name → build_registry raises.

    Exercises the collision discipline through the *real* discovery glob:
    distinct filenames but the same frontmatter ``name:`` in one root is
    ambiguous and is an error (mirrors ``skills/registry.py``).
    """
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    # Two files, distinct stems, both declaring `name: dlt-engineer`.
    _write(user_dir, "a.md", _USER_OVERRIDE)
    _write(user_dir, "b.md", _USER_OVERRIDE)

    discovery = AgentDiscovery.for_project(
        agents_dir=user_dir, builtin_dir=builtin_dir
    )
    with pytest.raises(ValueError, match="Duplicate agent name"):
        discovery.build_registry()


_MALFORMED = """\
---
name: broken
description: missing closing fence and bad shape
max_mode: not_a_real_mode
"""


def test_malformed_file_is_skipped_and_logged_not_fatal(tmp_path: Path) -> None:
    """One bad user file is skipped (logged); the rest still discover.

    Ratifies the silent-skip design: a per-file load failure must not take
    down discovery of every other agent. The skip is *logged* (a warning),
    not swallowed silently, so an operator can find it.
    """
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    _write(user_dir, "good.md", _USER_OVERRIDE)  # name: dlt-engineer
    _write(user_dir, "broken.md", _MALFORMED)

    discovery = AgentDiscovery.for_project(
        agents_dir=user_dir, builtin_dir=builtin_dir
    )
    records = _capture("carve.core.agents.discovery", logging.WARNING)
    registry = discovery.build_registry()

    # The good agent is discovered; the broken one is absent.
    assert "dlt-engineer" in registry
    assert "broken" not in registry
    # The skip was logged (not silent).
    assert any(
        "Skipping agent file" in rec.getMessage()
        and "broken.md" in rec.getMessage()
        for rec in records
    )


# A built-in doc (README.md) has no frontmatter fence and is NOT an agent.
_README = """\
# Built-in agent definitions

This directory documents the built-in discovery root. It is not an agent.
"""


def test_builtin_readme_is_skipped_quietly(tmp_path: Path) -> None:
    """A non-frontmatter README in the built-in root is skipped silently.

    NOTE 6: `builtin/README.md` documents the discovery root; the loader
    would reject it (no `---` fence) and `_parse_cached` would log a warning
    on EVERY build_registry() (carve agents list/show). The built-in glob
    skips frontmatter-less files quietly so the registry build is quiet.
    """
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    _write(builtin_dir, "README.md", _README)
    _write(builtin_dir, "dlt-engineer.md", _BUILTIN)
    user_dir.mkdir(parents=True, exist_ok=True)

    discovery = AgentDiscovery.for_project(
        agents_dir=user_dir, builtin_dir=builtin_dir
    )
    records = _capture("carve.core.agents.discovery", logging.WARNING)
    registry = discovery.build_registry()

    # The real agent is discovered; the README produced no agent.
    assert "dlt-engineer" in registry
    assert registry.names() == ["dlt-engineer"]
    # And it was quiet — no "Skipping agent file" warning for the README.
    assert not any(
        "README.md" in rec.getMessage() for rec in records
    )


def test_user_non_frontmatter_file_is_still_logged(tmp_path: Path) -> None:
    """A frontmatter-less file in the USER root keeps the skip-and-log rule.

    The quiet-skip is built-in-root only: a malformed user drop-in must
    still surface (the operator may have fumbled an agent file).
    """
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    _write(user_dir, "notes.md", _README)  # no frontmatter fence

    discovery = AgentDiscovery.for_project(
        agents_dir=user_dir, builtin_dir=builtin_dir
    )
    records = _capture("carve.core.agents.discovery", logging.WARNING)
    registry = discovery.build_registry()

    assert registry.names() == []
    assert any(
        "Skipping agent file" in rec.getMessage()
        and "notes.md" in rec.getMessage()
        for rec in records
    )


def _bump_mtime(path: Path) -> None:
    """Force the mtime forward so the (mtime, parsed) cache invalidates."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
