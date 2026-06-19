"""Unit tests for the safe declarative-agent loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.loader import (
    MAX_AGENT_FILE_BYTES,
    AgentLoadError,
    load_agent_file,
)
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.subagent_registry import (
    SubagentRegistry,
    spec_from_agent_file,
)

_VALID = """\
---
name: dlt-engineer
description: Authors and runs dlt sources. Use for ingest goals.
model: claude-sonnet-4-5-20250929
tools: [edit, create_file, bash, grep]
allowed_paths: ["el/**"]
max_mode: build
classifications: [new_pipeline, modify_pipeline]
---
You are the dlt engineer. Build and run dlt pipelines.
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_agent_parses_all_fields(tmp_path: Path) -> None:
    agent = load_agent_file(_write(tmp_path, "dlt-engineer.md", _VALID))
    assert agent.name == "dlt-engineer"
    assert agent.description.startswith("Authors and runs dlt")
    assert agent.model == "claude-sonnet-4-5-20250929"
    assert agent.tools == ("edit", "create_file", "bash", "grep")
    assert agent.allowed_paths == ("el/**",)
    assert agent.classifications == ("new_pipeline", "modify_pipeline")
    assert "dlt engineer" in agent.body


def test_max_mode_key_maps_to_capability(tmp_path: Path) -> None:
    """Field-name reconciliation: `max_mode:` → AgentSpec.capability."""
    agent = load_agent_file(_write(tmp_path, "a.md", _VALID))
    assert agent.max_mode is PermissionMode.BUILD
    spec = spec_from_agent_file(agent)
    assert spec.capability is PermissionMode.BUILD


def test_model_parses_and_absence_is_none(tmp_path: Path) -> None:
    with_model = load_agent_file(_write(tmp_path, "a.md", _VALID))
    assert spec_from_agent_file(with_model).model == "claude-sonnet-4-5-20250929"

    no_model_text = _VALID.replace(
        "model: claude-sonnet-4-5-20250929\n", ""
    )
    no_model = load_agent_file(_write(tmp_path, "b.md", no_model_text))
    assert no_model.model is None
    assert spec_from_agent_file(no_model).model is None


def test_malformed_yaml_fails_the_load(tmp_path: Path) -> None:
    bad = "---\nname: [unterminated\n---\nbody\n"
    with pytest.raises(AgentLoadError):
        load_agent_file(_write(tmp_path, "bad.md", bad))


def test_missing_fence_fails_the_load(tmp_path: Path) -> None:
    with pytest.raises(AgentLoadError):
        load_agent_file(_write(tmp_path, "nofence.md", "no frontmatter here"))


def test_missing_required_name_fails(tmp_path: Path) -> None:
    text = "---\ndescription: x\nmax_mode: read_only\n---\nbody\n"
    with pytest.raises(AgentLoadError):
        load_agent_file(_write(tmp_path, "noname.md", text))


def test_unknown_max_mode_fails(tmp_path: Path) -> None:
    text = _VALID.replace("max_mode: build", "max_mode: superuser")
    with pytest.raises(AgentLoadError):
        load_agent_file(_write(tmp_path, "badmode.md", text))


def test_unsafe_yaml_tag_does_not_construct_objects(tmp_path: Path) -> None:
    """A `!!python/object` tag must raise, never instantiate anything."""
    text = (
        "---\n"
        "name: x\n"
        "description: d\n"
        "max_mode: read_only\n"
        "evil: !!python/object/apply:os.system ['touch /tmp/carve_pwned']\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(AgentLoadError):
        load_agent_file(_write(tmp_path, "evil.md", text))


def test_oversized_file_fails_with_no_partial_register(tmp_path: Path) -> None:
    """A file over MAX_AGENT_FILE_BYTES fails the load; nothing registers."""
    # Build a file whose body pushes it just over the limit.
    filler = "x" * (MAX_AGENT_FILE_BYTES + 1)
    text = (
        "---\nname: big\ndescription: d\nmax_mode: read_only\n---\n" + filler
    )
    path = _write(tmp_path, "big.md", text)
    assert path.stat().st_size > MAX_AGENT_FILE_BYTES

    registry = SubagentRegistry()
    with pytest.raises(AgentLoadError):
        agent = load_agent_file(path)
        registry.register_files([agent])  # never reached
    # No partial register.
    assert registry.names() == []


def test_just_under_limit_loads(tmp_path: Path) -> None:
    header = "---\nname: ok\ndescription: d\nmax_mode: read_only\n---\n"
    body_len = MAX_AGENT_FILE_BYTES - len(header.encode("utf-8")) - 10
    text = header + ("y" * body_len)
    path = _write(tmp_path, "ok.md", text)
    assert path.stat().st_size <= MAX_AGENT_FILE_BYTES
    agent = load_agent_file(path)
    assert agent.name == "ok"


def test_duplicate_name_within_a_root_errors(tmp_path: Path) -> None:
    a = load_agent_file(_write(tmp_path, "a.md", _VALID))
    b_text = _VALID.replace("classifications:", "classifications:")  # same name
    b = load_agent_file(_write(tmp_path, "b.md", b_text))
    registry = SubagentRegistry()
    with pytest.raises(ValueError, match="Duplicate agent name"):
        registry.register_files([a, b], root_label="user")
