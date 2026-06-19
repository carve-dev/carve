"""Tests for `ProjectPaths` — the fixed control-plane dirs.

*(layout spec Tests: supports unit bullet 2's `ProjectPaths` half)*
"""

from __future__ import annotations

import os
from pathlib import Path

from carve.core.config.paths import ProjectPaths


def test_resolves_five_fixed_dirs_from_root(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)
    assert paths.root == tmp_path.resolve()
    assert paths.carve_dir == tmp_path.resolve() / "carve"
    assert paths.el_dir == tmp_path.resolve() / "el"
    assert paths.pipelines_dir == tmp_path.resolve() / "pipelines"
    assert paths.scratch_dir == tmp_path.resolve() / ".carve"
    assert paths.dlt_config_dir == tmp_path.resolve() / ".dlt"


def test_workspaces_dir_under_scratch(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)
    assert paths.workspaces_dir == paths.scratch_dir / "workspaces"


def test_all_dirs_absolute_even_from_relative_root(tmp_path: Path) -> None:
    # Resolve a relative root against tmp_path so the test doesn't depend
    # on (or pollute) the real cwd.
    rel = os.path.relpath(tmp_path, Path.cwd())
    paths = ProjectPaths.from_root(Path(rel))
    assert paths.root.is_absolute()
    assert paths.el_dir.is_absolute()
    assert paths.scratch_dir.is_absolute()
    assert paths.root == tmp_path.resolve()


def test_from_root_does_no_io(tmp_path: Path) -> None:
    target = tmp_path / "does-not-exist"
    paths = ProjectPaths.from_root(target)
    # Pure path math: none of the derived dirs are created.
    assert not paths.root.exists()
    assert not paths.el_dir.exists()


def test_frozen_dataclass_is_immutable(tmp_path: Path) -> None:
    import dataclasses

    import pytest

    paths = ProjectPaths.from_root(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        paths.root = Path("/elsewhere")  # type: ignore[misc]
