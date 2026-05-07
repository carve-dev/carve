"""Tests for ``carve.core.targets.registry``."""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.targets.registry import (
    InvalidTargetNameError,
    TargetExistsError,
    TargetNotFoundError,
    add_env_example_block,
    add_target_section,
    add_target_to_project,
    env_example_has_block,
    list_target_sections,
    remove_env_example_block,
    remove_target_section,
    rename_env_example_block,
    rename_target_section,
    section_referenced_env_vars,
    show_section_values,
    validate_target_name,
)

# ---------------------------------------------------------------------------
# Naming validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["dev", "prod", "staging", "eu_prod_2", "a", "x_1_y"])
def test_validate_target_name_accepts_valid(name: str) -> None:
    validate_target_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Dev",  # uppercase
        "1dev",  # leading digit
        "_dev",  # leading underscore
        "dev-prod",  # hyphen
        "dev prod",  # space
        "dev.prod",  # dot
    ],
)
def test_validate_target_name_rejects_invalid(name: str) -> None:
    with pytest.raises(InvalidTargetNameError):
        validate_target_name(name)


# ---------------------------------------------------------------------------
# add_target_section + tomlkit preservation
# ---------------------------------------------------------------------------


def test_add_target_section_creates_file(tmp_path: Path) -> None:
    """Append into a missing file creates it and writes a snowflake table."""
    conn = tmp_path / "carve" / "connections.toml"
    add_target_section("dev", conn)

    text = conn.read_text(encoding="utf-8")
    assert "[snowflake.dev]" in text
    assert 'account = "${DEV_SNOWFLAKE_ACCOUNT}"' in text
    assert 'user = "${DEV_SNOWFLAKE_USER}"' in text
    assert 'password = "${DEV_SNOWFLAKE_PASSWORD}"' in text
    assert 'role = "${DEV_SNOWFLAKE_ROLE}"' in text
    assert 'warehouse = "${DEV_SNOWFLAKE_WAREHOUSE}"' in text
    assert 'database = "${DEV_SNOWFLAKE_DATABASE}"' in text


def test_add_target_section_preserves_comments_and_order(tmp_path: Path) -> None:
    """Existing sections + comments remain byte-identical after append."""
    conn = tmp_path / "connections.toml"
    original = """\
# A user-written header comment.
# Multiple lines.

[snowflake.dev]
account = "literal-acc"
user = "literal-user"
password = "literal-password"
role = "TRANSFORMER"
warehouse = "WH"
database = "DB"
# trailing comment in dev
"""
    conn.write_text(original, encoding="utf-8")

    add_target_section("staging", conn)

    text = conn.read_text(encoding="utf-8")
    # Original header survives.
    assert "# A user-written header comment." in text
    assert "# Multiple lines." in text
    # Original section's literal values survive.
    assert 'account = "literal-acc"' in text
    assert 'user = "literal-user"' in text
    # Dev section comes before staging (append order).
    assert text.index("[snowflake.dev]") < text.index("[snowflake.staging]")
    # New staging section uses placeholders.
    assert 'account = "${STAGING_SNOWFLAKE_ACCOUNT}"' in text


def test_add_target_section_refuses_duplicate(tmp_path: Path) -> None:
    """Appending an existing section without ``force`` raises."""
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    with pytest.raises(TargetExistsError):
        add_target_section("dev", conn)


def test_add_target_section_force_overwrites(tmp_path: Path) -> None:
    """``force=True`` rewrites the section."""
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    add_target_section("dev", conn, force=True)
    # Still only one occurrence of the header.
    assert conn.read_text(encoding="utf-8").count("[snowflake.dev]") == 1


def test_list_target_sections_lists_all(tmp_path: Path) -> None:
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    add_target_section("prod", conn)
    add_target_section("staging", conn)
    assert sorted(list_target_sections(conn)) == ["dev", "prod", "staging"]


def test_list_target_sections_missing_file(tmp_path: Path) -> None:
    assert list_target_sections(tmp_path / "nope.toml") == []


def test_remove_target_section(tmp_path: Path) -> None:
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    add_target_section("prod", conn)
    remove_target_section("dev", conn)
    assert list_target_sections(conn) == ["prod"]
    text = conn.read_text(encoding="utf-8")
    assert "[snowflake.dev]" not in text


def test_remove_target_section_missing(tmp_path: Path) -> None:
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    with pytest.raises(TargetNotFoundError):
        remove_target_section("nope", conn)


def test_rename_target_section_rewrites_placeholders(tmp_path: Path) -> None:
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    rename_target_section("dev", "staging", conn)
    text = conn.read_text(encoding="utf-8")
    assert "[snowflake.dev]" not in text
    assert "[snowflake.staging]" in text
    assert 'account = "${STAGING_SNOWFLAKE_ACCOUNT}"' in text


def test_rename_target_section_refuses_dest_exists(tmp_path: Path) -> None:
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    add_target_section("staging", conn)
    with pytest.raises(TargetExistsError):
        rename_target_section("dev", "staging", conn)


def test_rename_target_section_missing(tmp_path: Path) -> None:
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    with pytest.raises(TargetNotFoundError):
        rename_target_section("staging", "qa", conn)


# ---------------------------------------------------------------------------
# show_section_values + section_referenced_env_vars
# ---------------------------------------------------------------------------


def test_show_section_values_marks_env_vars(tmp_path: Path) -> None:
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    values = show_section_values("dev", conn)
    keys = {v.key for v in values}
    assert {"account", "user", "password", "role", "warehouse", "database"} <= keys
    for v in values:
        # Every field is a single ${VAR} placeholder; env_var must be set.
        assert v.env_var is not None
        assert v.env_var.startswith("DEV_SNOWFLAKE_")


def test_show_section_values_literals_have_no_env_var(tmp_path: Path) -> None:
    """Literal values render with ``env_var is None``."""
    conn = tmp_path / "connections.toml"
    conn.write_text(
        """\
[snowflake.dev]
account = "literal"
user = "${DEV_SNOWFLAKE_USER}"
password = "p"
role = "r"
warehouse = "w"
database = "d"
""",
        encoding="utf-8",
    )
    by_key = {v.key: v for v in show_section_values("dev", conn)}
    assert by_key["account"].env_var is None
    assert by_key["account"].raw == "literal"
    assert by_key["user"].env_var == "DEV_SNOWFLAKE_USER"


def test_section_referenced_env_vars(tmp_path: Path) -> None:
    conn = tmp_path / "connections.toml"
    add_target_section("dev", conn)
    vars_ = section_referenced_env_vars("dev", conn)
    assert "DEV_SNOWFLAKE_ACCOUNT" in vars_
    assert "DEV_SNOWFLAKE_USER" in vars_


# ---------------------------------------------------------------------------
# .env.example helpers
# ---------------------------------------------------------------------------


def test_add_env_example_block_creates_file(tmp_path: Path) -> None:
    env = tmp_path / ".env.example"
    add_env_example_block("staging", env)
    text = env.read_text(encoding="utf-8")
    assert "# === staging target ===" in text
    assert "STAGING_SNOWFLAKE_ACCOUNT=" in text
    assert "STAGING_SNOWFLAKE_USER=" in text
    assert "STAGING_SNOWFLAKE_PASSWORD=" in text
    assert "STAGING_SNOWFLAKE_ROLE=" in text
    assert "STAGING_SNOWFLAKE_WAREHOUSE=" in text
    assert "STAGING_SNOWFLAKE_DATABASE=" in text


def test_add_env_example_block_appends_with_separator(tmp_path: Path) -> None:
    env = tmp_path / ".env.example"
    env.write_text("# Pre-existing content\nKEY=value\n", encoding="utf-8")
    add_env_example_block("dev", env)
    text = env.read_text(encoding="utf-8")
    assert "# Pre-existing content" in text
    assert "KEY=value" in text
    assert "# === dev target ===" in text
    # Pre-existing content comes first.
    assert text.index("KEY=value") < text.index("# === dev target ===")


def test_env_example_has_block(tmp_path: Path) -> None:
    env = tmp_path / ".env.example"
    assert not env_example_has_block("dev", env)
    add_env_example_block("dev", env)
    assert env_example_has_block("dev", env)
    assert not env_example_has_block("staging", env)


def test_remove_env_example_block(tmp_path: Path) -> None:
    env = tmp_path / ".env.example"
    add_env_example_block("dev", env)
    add_env_example_block("staging", env)
    add_env_example_block("prod", env)
    remove_env_example_block("staging", env)
    text = env.read_text(encoding="utf-8")
    assert "# === dev target ===" in text
    assert "# === staging target ===" not in text
    assert "STAGING_SNOWFLAKE_ACCOUNT=" not in text
    assert "# === prod target ===" in text


def test_rename_env_example_block(tmp_path: Path) -> None:
    env = tmp_path / ".env.example"
    add_env_example_block("dev", env)
    rename_env_example_block("dev", "staging", env)
    text = env.read_text(encoding="utf-8")
    assert "# === dev target ===" not in text
    assert "# === staging target ===" in text
    assert "DEV_SNOWFLAKE_USER=" not in text
    assert "STAGING_SNOWFLAKE_USER=" in text


# ---------------------------------------------------------------------------
# add_target_to_project — high-level orchestrator
# ---------------------------------------------------------------------------


def test_add_target_to_project_writes_three_artifacts(tmp_path: Path) -> None:
    """The orchestrator touches connections.toml, .env.example, and targets/."""
    add_target_to_project("dev", tmp_path)
    conn = tmp_path / "carve" / "connections.toml"
    env = tmp_path / ".env.example"
    el_dir = tmp_path / "targets" / "dev" / "el"

    assert conn.is_file()
    assert "[snowflake.dev]" in conn.read_text(encoding="utf-8")
    assert env.is_file()
    assert "# === dev target ===" in env.read_text(encoding="utf-8")
    assert el_dir.is_dir()


def test_add_target_to_project_invalid_name_raises(tmp_path: Path) -> None:
    with pytest.raises(InvalidTargetNameError):
        add_target_to_project("Dev", tmp_path)


def test_add_target_to_project_idempotent_on_env_block(tmp_path: Path) -> None:
    """``.env.example`` block isn't appended twice for the same target."""
    add_target_to_project("dev", tmp_path)
    # Force=True so the section can be re-added; the env-example block should
    # still only appear once.
    add_target_to_project("dev", tmp_path, force=True)
    text = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert text.count("# === dev target ===") == 1
