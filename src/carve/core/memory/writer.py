"""Write project-memory files.

Lean scope: only :meth:`MemoryWriter.append_decision`, which is the one write
that is safe without the plan/build gate (it appends an immutable, dated record
of a decision the team already made). The ``plan_id``-gated ``standards`` /
sidecar writes are deferred — the Plan/Build state model can't yet express the
spec's "plan exists and is built, not yet deployed" gate, and no plan/build
flow produces a valid ``plan_id`` for a memory edit until that lands. Until
then, `carve memory edit` writes the file directly.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import date as date_cls
from pathlib import Path

from carve.core.config.paths import ProjectPaths
from carve.core.memory.loader import MemoryLoader

# A real decision entry heading: "## YYYY-MM-DD — <title>". Used to anchor
# both newest-first insertion and duplicate detection to actual entries, so
# the scaffolded "## Format" docs heading is never mistaken for an entry.
_DATED_HEADING_RE = re.compile(r"^## \d{4}-\d{2}-\d{2} — ")


class DecisionAlreadyExists(Exception):
    """A decision with the same title + date already exists in decisions.md."""


class MemoryWriter:
    """Writes to the project-memory files (append-decision in the lean core)."""

    def __init__(self, paths: ProjectPaths, loader: MemoryLoader | None = None) -> None:
        self._paths = paths
        # The loader is optional; when present it's invalidated after a write so
        # the next read picks up the new contents within the same mtime tick.
        self._loader = loader

    def append_decision(
        self,
        *,
        date: date_cls,
        title: str,
        body: str,
        reviewers: list[str],
        force: bool = False,
    ) -> Path:
        """Append a formatted entry to ``carve/decisions.md`` (newest-first).

        Idempotency: two appends with the same title on the same date raise
        :class:`DecisionAlreadyExists` unless ``force=True``.

        Raises :class:`ValueError` if ``title`` spans multiple lines — a
        newline would otherwise forge extra ``## `` headings in the file.
        """
        title = title.strip()
        if not title:
            raise ValueError("Decision title must not be empty.")
        if "\n" in title or "\r" in title:
            raise ValueError("Decision title must be a single line (no newlines).")

        path = self._paths.carve_dir / "decisions.md"
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""

        heading = _decision_heading(date, title)
        if not force and _has_entry(existing, heading):
            raise DecisionAlreadyExists(
                f'A decision titled "{title}" dated {date.isoformat()} already '
                "exists in decisions.md. Use force=True to add it anyway."
            )

        entry = _format_entry(heading, body, reviewers)
        updated = _insert_newest_first(existing, entry)

        _atomic_write(path, updated)
        if self._loader is not None:
            self._loader.invalidate(path)
        return path


def _decision_heading(date: date_cls, title: str) -> str:
    return f"## {date.isoformat()} — {title}"


def _has_entry(existing: str, heading: str) -> bool:
    """Whether a decision with this exact heading already exists.

    Matches the heading on its own line. The scaffolded template's indented
    ``## YYYY-MM-DD`` example never matches (it's indented + not a real date).
    A user-written body line that is *byte-identical* to a dated heading could
    in principle false-match — an inherent property of free-form markdown that
    we accept (force=True is the escape hatch).
    """
    return any(line.rstrip() == heading for line in existing.splitlines())


def _format_entry(heading: str, body: str, reviewers: list[str]) -> str:
    lines = [heading, "", body.strip()]
    cleaned = [r.strip() for r in reviewers if r.strip()]
    if cleaned:
        lines += ["", f"**Reviewers:** {', '.join(cleaned)}"]
    return "\n".join(lines) + "\n"


def _insert_newest_first(existing: str, entry: str) -> str:
    """Insert ``entry`` above the first existing **dated** entry (newest-first).

    Anchoring on dated-entry headings (not "any ``## ``") keeps the scaffolded
    ``## Format`` docs block and any other section above the entries region.
    When there is no prior dated entry, the entry is appended below the
    header/template region.
    """
    block = entry if entry.endswith("\n") else entry + "\n"
    if not existing.strip():
        return block
    lines = existing.splitlines(keepends=True)
    insert_at = next((i for i, line in enumerate(lines) if _DATED_HEADING_RE.match(line)), None)
    if insert_at is None:
        joined = "".join(lines)
        if not joined.endswith("\n"):
            joined += "\n"
        return joined + "\n" + block
    head = "".join(lines[:insert_at])
    tail = "".join(lines[insert_at:])
    return f"{head}{block}\n{tail}"


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + ``os.replace``).

    Prevents a crash/disk-full mid-write from truncating an existing
    decisions.md: the swap is atomic within a filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".decisions-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


__all__ = ["DecisionAlreadyExists", "MemoryWriter"]
