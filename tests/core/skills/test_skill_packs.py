"""Skill-pack tests: discovery, description-match, no-exec-at-load."""

from __future__ import annotations

import shutil
from pathlib import Path

from carve.core.skills.pack_discovery import discover_pack_roots
from carve.core.skills.packs import load_skill_pack

_FIXTURE_PACK = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "skill_packs"
    / "_example"
)


def _isolated_skills_dir(tmp_path: Path) -> Path:
    """Copy the fixture pack into a temp skills dir (so the marker, if ever
    written, lands in the temp tree and we never dirty the repo fixture)."""
    skills_dir = tmp_path / "skills"
    shutil.copytree(_FIXTURE_PACK, skills_dir / "_example")
    return skills_dir


def test_pack_loads_with_all_fields(tmp_path: Path) -> None:
    skills_dir = _isolated_skills_dir(tmp_path)
    pack = load_skill_pack(skills_dir / "_example")
    assert pack.name == "_example"
    assert "self-contained" in pack.description
    assert pack.expects_env == ("EXAMPLE_API_KEY",)
    assert "Example skill pack" in pack.instructions
    # The bundled script path is recorded (inert) but not read/run.
    assert any(p.name == "side_effect.py" for p in pack.script_paths)


def test_pack_offered_on_description_match(tmp_path: Path) -> None:
    skills_dir = _isolated_skills_dir(tmp_path)
    library = discover_pack_roots(skills_dir=skills_dir)
    matches = library.match("example")
    assert any(m.name == "_example" for m in matches)
    # The lookup tool injects the instructions on demand.
    tool = library.make_lookup_tool()
    content = tool.executor({"pack_name": "_example"})
    assert isinstance(content, str)
    assert "Example skill pack" in content


def test_bundled_script_is_not_executed_at_load(tmp_path: Path) -> None:
    """Loading the pack must NOT run side_effect.py (RCE-on-discovery)."""
    skills_dir = _isolated_skills_dir(tmp_path)
    marker = skills_dir / "_example" / "scripts" / "EXECUTED_MARKER"

    # Discover + load + render content — the whole pipeline.
    library = discover_pack_roots(skills_dir=skills_dir)
    library.discover()
    load_skill_pack(skills_dir / "_example")
    library.make_lookup_tool().executor({"pack_name": "_example"})

    assert not marker.exists(), (
        "Bundled script ran at load — RCE-on-discovery; loading must be inert."
    )


def test_unknown_pack_name_is_rejected(tmp_path: Path) -> None:
    skills_dir = _isolated_skills_dir(tmp_path)
    library = discover_pack_roots(skills_dir=skills_dir)
    tool = library.make_lookup_tool()
    try:
        tool.executor({"pack_name": "does-not-exist"})
    except Exception as exc:  # ToolExecutionError
        assert "Unknown skill pack" in str(exc)
    else:
        raise AssertionError("expected an error for an unknown pack name")
