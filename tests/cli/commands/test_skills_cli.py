"""Tests for ``carve skills`` (list / show / test)."""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from carve.cli.main import app

runner = CliRunner()

_FIXTURE_PACK = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "skill_packs"
    / "_example"
)


def _project_with_pack(tmp_path: Path) -> Path:
    skills_dir = tmp_path / "carve" / "skills"
    shutil.copytree(_FIXTURE_PACK, skills_dir / "_example")
    return tmp_path


def test_skills_list_shows_builtin_and_pack(tmp_path: Path) -> None:
    _project_with_pack(tmp_path)
    result = runner.invoke(app, ["skills", "list", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    # A built-in catalog skill and the fixture pack both appear.
    assert "_example" in result.output
    assert "pack" in result.output
    assert "built-in" in result.output


def test_skills_show_pack_prints_instructions(tmp_path: Path) -> None:
    _project_with_pack(tmp_path)
    result = runner.invoke(
        app, ["skills", "show", "_example", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "Example skill pack" in result.output


def test_skills_test_loads_pack_without_exec(tmp_path: Path) -> None:
    project = _project_with_pack(tmp_path)
    marker = project / "carve" / "skills" / "_example" / "scripts" / "EXECUTED_MARKER"
    result = runner.invoke(
        app, ["skills", "test", "_example", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "loaded OK" in result.output
    assert not marker.exists()  # no exec at load


def test_skills_show_unknown_fails(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["skills", "show", "ghost", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
