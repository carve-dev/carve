"""Detection: dbt / dlt / git / docker / re-init across directory shapes."""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.init.detect import detect


def test_greenfield_detects_nothing(tmp_path: Path) -> None:
    d = detect(tmp_path)
    assert d.dbt_projects == ()
    assert d.dlt_present is False
    assert d.re_init is False
    assert d.has_git is False


def test_detects_dbt_at_root(tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text("name: analytics\n")
    d = detect(tmp_path)
    assert d.dbt_projects == (tmp_path / "dbt_project.yml",)


def test_detects_dbt_one_level_down(tmp_path: Path) -> None:
    sub = tmp_path / "transform"
    sub.mkdir()
    (sub / "dbt_project.yml").write_text("name: analytics\n")
    d = detect(tmp_path)
    assert d.dbt_projects == (sub / "dbt_project.yml",)


def test_detects_multiple_dbt_projects(tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text("name: a\n")
    sub = tmp_path / "more"
    sub.mkdir()
    (sub / "dbt_project.yml").write_text("name: b\n")
    d = detect(tmp_path)
    assert len(d.dbt_projects) == 2


def test_ignores_dbt_two_levels_down(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "dbt_project.yml").write_text("name: too_deep\n")
    assert detect(tmp_path).dbt_projects == ()


def test_detects_dlt_via_dot_dlt_dir(tmp_path: Path) -> None:
    (tmp_path / ".dlt").mkdir()
    assert detect(tmp_path).dlt_present is True


def test_detects_dlt_via_el_import(tmp_path: Path) -> None:
    pkg = tmp_path / "el" / "stripe"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("import dlt\n\n@dlt.source\ndef s():\n    ...\n")
    assert detect(tmp_path).dlt_present is True


def test_no_dlt_when_el_has_no_dlt_import(tmp_path: Path) -> None:
    pkg = tmp_path / "el" / "notdlt"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("import os\nx = 1\n")
    assert detect(tmp_path).dlt_present is False


def test_dlt_detection_tolerates_unparseable_python(tmp_path: Path) -> None:
    pkg = tmp_path / "el" / "broken"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("this is not (valid python\n")
    # Doesn't raise; just doesn't detect dlt in the broken file.
    assert detect(tmp_path).dlt_present is False


def test_re_init_when_carve_toml_present(tmp_path: Path) -> None:
    (tmp_path / "carve.toml").write_text("[project]\nname = 'x'\n")
    assert detect(tmp_path).re_init is True


def test_detects_git(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert detect(tmp_path).has_git is True


def test_has_docker_reflects_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # detect() looks up shutil.which at call time, so patching the stdlib
    # module's attribute is enough (and dodges the package re-export shadow).
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    assert detect(tmp_path).has_docker is True
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert detect(tmp_path).has_docker is False
