"""Read the project-memory files with mtime-based caching.

The single canonical reader for the five memory file types defined by the
layout: ``carve/conventions.md``, ``carve/standards.md``, ``carve/decisions.md``,
the per-pipeline sidecar ``pipelines/<name>.md``, and the per-el sidecar
``el/<name>/NOTES.md``. Every consumer that needs memory goes through this
loader rather than reading the files itself.

Caching mirrors the dbt-manifest mtime-watch pattern (ARCHITECTURE §6.3): an
in-process dict keyed by path holds ``(st_mtime, MemoryFile)``; a load re-reads
only when ``os.stat().st_mtime`` no longer matches the cached value. Writers
call :meth:`MemoryLoader.invalidate` after a write so the next read is fresh
even within a single mtime tick.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from carve.core.config.paths import ProjectPaths


@dataclass(frozen=True)
class MemoryFile:
    """One memory file's contents plus filesystem metadata."""

    path: Path
    contents: str
    mtime: datetime
    size_bytes: int


class MemoryLoader:
    """mtime-cached reader for the five memory file types."""

    def __init__(self, paths: ProjectPaths) -> None:
        self._paths = paths
        self._cache: dict[Path, tuple[float, MemoryFile]] = {}

    def load_conventions(self) -> MemoryFile | None:
        return self._load(self._paths.carve_dir / "conventions.md")

    def load_standards(self) -> MemoryFile | None:
        return self._load(self._paths.carve_dir / "standards.md")

    def load_decisions(self) -> MemoryFile | None:
        return self._load(self._paths.carve_dir / "decisions.md")

    def load_pipeline_sidecar(self, name: str) -> MemoryFile | None:
        return self._load(self._paths.pipelines_dir / f"{name}.md")

    def load_el_sidecar(self, name: str) -> MemoryFile | None:
        return self._load(self._paths.el_dir / name / "NOTES.md")

    def invalidate(self, path: Path | None = None) -> None:
        """Drop a cache entry (or the whole cache when ``path`` is None)."""
        if path is None:
            self._cache.clear()
        else:
            self._cache.pop(path, None)

    def _load(self, path: Path) -> MemoryFile | None:
        try:
            st = os.stat(path)
        except OSError:
            # Missing file (or unreadable dir component) → not present.
            self._cache.pop(path, None)
            return None
        cached = self._cache.get(path)
        if cached is not None and cached[0] == st.st_mtime:
            return cached[1]
        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # e.g. the path is a directory, or non-UTF-8 bytes.
            self._cache.pop(path, None)
            return None
        memory_file = MemoryFile(
            path=path,
            contents=contents,
            mtime=datetime.fromtimestamp(st.st_mtime, tz=UTC),
            size_bytes=st.st_size,
        )
        self._cache[path] = (st.st_mtime, memory_file)
        return memory_file


__all__ = ["MemoryFile", "MemoryLoader"]
