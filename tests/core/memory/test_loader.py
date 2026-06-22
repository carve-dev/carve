"""MemoryLoader: mtime-based caching, invalidation, absent files, sidecars."""

from __future__ import annotations

import os
from pathlib import Path

from carve.core.config.paths import ProjectPaths
from carve.core.memory.loader import MemoryLoader


def _loader(root: Path) -> MemoryLoader:
    (root / "carve").mkdir(parents=True, exist_ok=True)
    return MemoryLoader(ProjectPaths.from_root(root))


def test_loads_the_three_core_files(tmp_path: Path) -> None:
    (tmp_path / "carve" / "conventions.md").parent.mkdir(parents=True)
    (tmp_path / "carve" / "conventions.md").write_text("conv\n")
    (tmp_path / "carve" / "standards.md").write_text("std\n")
    (tmp_path / "carve" / "decisions.md").write_text("dec\n")
    loader = MemoryLoader(ProjectPaths.from_root(tmp_path))

    assert loader.load_conventions().contents == "conv\n"
    assert loader.load_standards().contents == "std\n"
    assert loader.load_decisions().contents == "dec\n"


def test_absent_files_return_none(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    assert loader.load_conventions() is None
    assert loader.load_pipeline_sidecar("nope") is None
    assert loader.load_el_sidecar("nope") is None


def test_pipeline_and_el_sidecars(tmp_path: Path) -> None:
    (tmp_path / "pipelines").mkdir(parents=True)
    (tmp_path / "pipelines" / "stripe.md").write_text("pipe notes\n")
    (tmp_path / "el" / "orders").mkdir(parents=True)
    (tmp_path / "el" / "orders" / "NOTES.md").write_text("el notes\n")
    loader = MemoryLoader(ProjectPaths.from_root(tmp_path))

    assert loader.load_pipeline_sidecar("stripe").contents == "pipe notes\n"
    assert loader.load_el_sidecar("orders").contents == "el notes\n"


def test_caches_by_mtime_and_rereads_on_change(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    f = tmp_path / "carve" / "standards.md"
    f.write_text("A\n")
    os.utime(f, (1000, 1000))
    assert loader.load_standards().contents == "A\n"

    # Change contents but keep the SAME mtime → cached value is returned.
    f.write_text("B\n")
    os.utime(f, (1000, 1000))
    assert loader.load_standards().contents == "A\n"  # stale-by-design: mtime unchanged

    # Bump the mtime → the loader re-reads.
    f.write_text("C\n")
    os.utime(f, (2000, 2000))
    assert loader.load_standards().contents == "C\n"
    assert loader.load_standards().size_bytes == 2


def test_invalidate_forces_reread(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    f = tmp_path / "carve" / "standards.md"
    f.write_text("A\n")
    os.utime(f, (1000, 1000))
    assert loader.load_standards().contents == "A\n"

    f.write_text("B\n")
    os.utime(f, (1000, 1000))  # same mtime — would normally be cached
    loader.invalidate(f)
    assert loader.load_standards().contents == "B\n"

    # invalidate() with no arg clears everything.
    f.write_text("C\n")
    os.utime(f, (1000, 1000))
    loader.invalidate()
    assert loader.load_standards().contents == "C\n"


def test_directory_at_path_is_treated_as_absent(tmp_path: Path) -> None:
    # A directory where a file is expected must not crash; returns None.
    (tmp_path / "carve" / "standards.md").mkdir(parents=True)
    loader = MemoryLoader(ProjectPaths.from_root(tmp_path))
    assert loader.load_standards() is None
