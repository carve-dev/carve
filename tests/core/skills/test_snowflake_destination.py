"""Tests for the `snowflake_destination` skill markdown file.

Verifies that the structural anchors `lookup_skill` callers depend on
are present and that the DDL-emission contract from P1-06 is encoded
in the file (idempotency rules, allowed/forbidden statement forms,
section dividers).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SKILL_PATH = Path(__file__).resolve().parents[3] / "src/carve/core/skills/snowflake_destination.md"


@pytest.fixture
def skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def test_skill_markdown_exists() -> None:
    assert SKILL_PATH.is_file(), f"missing: {SKILL_PATH}"


def test_skill_has_top_heading(skill_text: str) -> None:
    assert skill_text.lstrip().startswith("# Skill: snowflake_destination")


@pytest.mark.parametrize(
    "anchor",
    [
        "## `executemany` quirks",
        "## `write_pandas`",
        "## `COPY INTO` from internal stage",
        "## `MERGE` upsert pattern",
        "## Role / warehouse propagation",
        "## Snowflake-specific types",
        "## DDL emission patterns (Pillar 1)",
    ],
)
def test_skill_has_section_anchor(skill_text: str, anchor: str) -> None:
    assert skill_text.count(anchor) == 1, (
        f"{anchor!r} should appear exactly once; found {skill_text.count(anchor)}"
    )


def test_skill_documents_section_dividers(skill_text: str) -> None:
    """The DDL emission contract names the three required dividers."""
    for divider in ("-- === Schema ===", "-- === Table ===", "-- === Grants ==="):
        assert divider in skill_text, f"missing divider: {divider}"


def test_skill_forbids_create_or_replace(skill_text: str) -> None:
    """The DDL section forbids `CREATE OR REPLACE`."""
    # The skill names the forbidden form explicitly so the agent has it
    # spelled out as an anti-pattern, not just absent.
    assert "CREATE OR REPLACE" in skill_text
    assert "forbidden" in skill_text.lower()


def test_skill_forbids_bare_rename(skill_text: str) -> None:
    """The DDL section forbids bare `RENAME`."""
    assert "RENAME" in skill_text
    # The contract should mention there's no idempotent form.
    assert "idempotent" in skill_text.lower()


def test_skill_documents_alter_add_column(skill_text: str) -> None:
    """The skill documents the additive ALTER path for modify flows."""
    assert "ALTER TABLE" in skill_text
    assert "ADD COLUMN IF NOT EXISTS" in skill_text


def test_skill_documents_grants_runtime_role(skill_text: str) -> None:
    """The skill explains the runtime-role grant the DDL must emit."""
    assert "GRANT" in skill_text
    assert "runtime_role" in skill_text or "TRANSFORMER_DEV" in skill_text
