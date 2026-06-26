"""MemoryWriter: append_decision + write_conventions (format, atomicity, cache)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.memory.loader import MemoryLoader
from carve.core.memory.writer import DecisionAlreadyExists, MemoryWriter
from carve.init.templates import CONVENTIONS_MD_CONTENT, DECISIONS_MD_CONTENT


def _scaffolded(root: Path) -> Path:
    (root / "carve").mkdir(parents=True)
    decisions = root / "carve" / "decisions.md"
    decisions.write_text(DECISIONS_MD_CONTENT, encoding="utf-8")
    return decisions


def test_append_formats_entry_with_heading_and_reviewers(tmp_path: Path) -> None:
    decisions = _scaffolded(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    writer.append_decision(
        date=date(2026, 4, 12),
        title="Stripe retention policy",
        body="Keep Stripe charges for 18 months.",
        reviewers=["alice@", "bob@"],
    )
    text = decisions.read_text()
    assert "## 2026-04-12 — Stripe retention policy" in text
    assert "Keep Stripe charges for 18 months." in text
    assert "**Reviewers:** alice@, bob@" in text


def test_append_inserts_newest_first(tmp_path: Path) -> None:
    decisions = _scaffolded(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    writer.append_decision(date=date(2026, 1, 1), title="First", body="a", reviewers=[])
    writer.append_decision(date=date(2026, 2, 2), title="Second", body="b", reviewers=[])
    text = decisions.read_text()
    # Newest (Second) appears before older (First) — but BELOW the template's
    # "## Format" docs section (entries anchor to the dated-entry region, not
    # the first '## ' heading), and the stale placeholder is gone.
    assert text.index("— Second") < text.index("— First")
    assert text.index("## Format") < text.index("— Second")
    assert "(No decisions recorded yet)" not in text


def test_duplicate_title_and_date_rejected_unless_forced(tmp_path: Path) -> None:
    _scaffolded(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    writer.append_decision(date=date(2026, 4, 12), title="Dup", body="x", reviewers=[])
    with pytest.raises(DecisionAlreadyExists):
        writer.append_decision(date=date(2026, 4, 12), title="Dup", body="y", reviewers=[])
    # force=True appends a second copy.
    writer.append_decision(date=date(2026, 4, 12), title="Dup", body="y", reviewers=[], force=True)
    assert (tmp_path / "carve" / "decisions.md").read_text().count("## 2026-04-12 — Dup") == 2


def test_same_title_different_date_is_allowed(tmp_path: Path) -> None:
    _scaffolded(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    writer.append_decision(date=date(2026, 4, 12), title="Recur", body="x", reviewers=[])
    writer.append_decision(
        date=date(2026, 5, 13), title="Recur", body="y", reviewers=[]
    )  # no raise


def test_append_invalidates_loader_cache(tmp_path: Path) -> None:
    decisions = _scaffolded(tmp_path)
    loader = MemoryLoader(ProjectPaths.from_root(tmp_path))
    before = loader.load_decisions().contents  # primes the cache
    assert "— Cache test" not in before

    writer = MemoryWriter(ProjectPaths.from_root(tmp_path), loader)
    writer.append_decision(date=date(2026, 6, 1), title="Cache test", body="z", reviewers=[])
    # Without invalidation a same-mtime-tick write could be masked; the writer
    # invalidates, so the next read is fresh.
    assert "— Cache test" in loader.load_decisions().contents
    assert decisions.read_text().count("— Cache test") == 1


def test_title_with_newline_is_rejected(tmp_path: Path) -> None:
    # A newline in the title would forge a second '## ' heading in the file.
    _scaffolded(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    with pytest.raises(ValueError):
        writer.append_decision(
            date=date(2026, 1, 1), title="Real\n## 2099-01-01 — Forged", body="x", reviewers=[]
        )


def test_empty_title_is_rejected(tmp_path: Path) -> None:
    _scaffolded(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    with pytest.raises(ValueError):
        writer.append_decision(date=date(2026, 1, 1), title="   ", body="x", reviewers=[])


def test_first_append_against_scaffold_lands_below_format(tmp_path: Path) -> None:
    # Regression for the bug where the first entry landed above '## Format'
    # and the "(No decisions recorded yet)" placeholder stayed forever.
    decisions = _scaffolded(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    writer.append_decision(date=date(2026, 3, 3), title="Only", body="x", reviewers=[])
    text = decisions.read_text()
    assert text.index("## Format") < text.index("## 2026-03-03 — Only")
    assert "(No decisions recorded yet)" not in text


def test_format_example_in_template_is_not_treated_as_duplicate(tmp_path: Path) -> None:
    # The scaffolded template has an indented "## YYYY-MM-DD — Short title"
    # example inside a code block; it must not collide with real headings.
    _scaffolded(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    writer.append_decision(
        date=date(2026, 7, 7), title="Short title", body="not the example", reviewers=[]
    )  # must not raise DecisionAlreadyExists


# ---------------------------------------------------------------------------
# write_conventions
# ---------------------------------------------------------------------------


def _scaffolded_conventions(root: Path) -> Path:
    """A project with the comment-only conventions.md placeholder (as init writes)."""
    (root / "carve").mkdir(parents=True, exist_ok=True)
    conv = root / "carve" / "conventions.md"
    conv.write_text(CONVENTIONS_MD_CONTENT, encoding="utf-8")
    return conv


def test_write_conventions_overwrites_placeholder(tmp_path: Path) -> None:
    conv = _scaffolded_conventions(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    path = writer.write_conventions("# Inferred project conventions\n\n- stg_ for staging\n")
    assert path == conv
    text = conv.read_text()
    assert "Inferred project conventions" in text
    # The comment-only placeholder is gone.
    assert "Inferred project conventions land here" not in text


def test_write_conventions_round_trips_through_loader(tmp_path: Path) -> None:
    _scaffolded_conventions(tmp_path)
    loader = MemoryLoader(ProjectPaths.from_root(tmp_path))
    primed = loader.load_conventions()  # primes the cache (placeholder)
    assert primed is not None
    # The placeholder is comment-only (no real markdown heading yet).
    assert "# Inferred project conventions" not in primed.contents

    writer = MemoryWriter(ProjectPaths.from_root(tmp_path), loader)
    body = "# Inferred project conventions\n\nstaging → view\n"
    writer.write_conventions(body)
    # Invalidation means the next read is fresh even within an mtime tick.
    loaded = loader.load_conventions()
    assert loaded is not None
    assert loaded.contents == body


def test_write_conventions_is_atomic_no_temp_left(tmp_path: Path) -> None:
    _scaffolded_conventions(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    writer.write_conventions("# C\n\nx\n")
    # No stray .conventions-*.tmp sibling after a clean write.
    leftovers = list((tmp_path / "carve").glob(".conventions-*.tmp"))
    assert leftovers == [], leftovers


def test_write_conventions_creates_carve_dir(tmp_path: Path) -> None:
    """No pre-existing carve/ → the writer creates it (parents=True)."""
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    path = writer.write_conventions("# C\n\nx\n")
    assert path.is_file()
    assert path.parent.name == "carve"


def test_write_conventions_rejects_empty(tmp_path: Path) -> None:
    """An empty body would falsely signal 'no conventions inferred' — rejected."""
    _scaffolded_conventions(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    with pytest.raises(ValueError):
        writer.write_conventions("   \n")


def test_write_conventions_is_idempotent(tmp_path: Path) -> None:
    _scaffolded_conventions(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    body = "# Inferred project conventions\n\nstaging → view\n"
    writer.write_conventions(body)
    writer.write_conventions(body)  # second run is safe
    assert (tmp_path / "carve" / "conventions.md").read_text() == body


def test_write_conventions_does_not_touch_decisions(tmp_path: Path) -> None:
    """The conventions write must not disturb append_decision's file."""
    _scaffolded(tmp_path)
    _scaffolded_conventions(tmp_path)
    writer = MemoryWriter(ProjectPaths.from_root(tmp_path))
    writer.append_decision(date=date(2026, 1, 1), title="D", body="x", reviewers=[])
    writer.write_conventions("# C\n\nx\n")
    decisions = (tmp_path / "carve" / "decisions.md").read_text()
    assert "## 2026-01-01 — D" in decisions
