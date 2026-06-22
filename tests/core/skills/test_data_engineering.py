"""Tests for the `data_engineering` skill markdown file.

The skill is content the extract-load agent loads on demand via the
`lookup_skill` tool. The tests verify that the file ships, parses (it
is markdown — we check it for the structural anchors `lookup_skill`
clients rely on), and contains the sub-section headers the prompt's
"available skills" block advertises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SKILL_PATH = Path(__file__).resolve().parents[3] / "src/carve/core/skills/data_engineering.md"


@pytest.fixture
def skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def test_skill_markdown_exists() -> None:
    assert SKILL_PATH.is_file(), f"missing: {SKILL_PATH}"


def test_skill_has_top_heading(skill_text: str) -> None:
    """The skill starts with a `# Skill: data_engineering` heading."""
    assert skill_text.lstrip().startswith("# Skill: data_engineering")


@pytest.mark.parametrize(
    "anchor",
    [
        "## Pagination patterns",
        "## Retry with exponential backoff",
        "## Watermark / incremental extraction",
        "## Idempotent writes",
        "## Memory-bounded streaming",
        "## Type coercion for JSON-ish nested data",
        "## Structured logging",
        "## Connection management",
    ],
)
def test_skill_has_section_anchor(skill_text: str, anchor: str) -> None:
    """Every advertised sub-section header is present and unique."""
    assert skill_text.count(anchor) == 1, (
        f"{anchor!r} should appear exactly once; found {skill_text.count(anchor)}"
    )


def test_skill_iowa_liquor_example_present(skill_text: str) -> None:
    """The Iowa-liquor regression context is documented in the skill."""
    assert "Iowa-liquor" in skill_text or "iowa" in skill_text.lower()


def test_skill_no_pandas_default(skill_text: str) -> None:
    """The skill does not advertise pandas as a default — minimality matters."""
    # The skill may discuss pandas in contrast to a no-pandas default; what
    # we want to avoid is a prescriptive "always include pandas" line.
    assert "always include pandas" not in skill_text.lower()
