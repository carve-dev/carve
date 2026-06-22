"""Unit tests for `carve.core.targets.destination`.

Covers the four pieces of the destination model:

* ``parse_fqn_from_goal`` — natural-language FQN extraction.
* ``write_destination_toml`` — override-vs-inherit rendering.
* ``read_destination_toml`` — round-trip + malformed-file rejection.
* ``resolve_at_runtime`` — the canonical resolution rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.targets.destination import (
    Destination,
    parse_fqn_from_goal,
    read_destination_toml,
    resolve_at_runtime,
    write_destination_toml,
)

# ---------------------------------------------------------------------------
# parse_fqn_from_goal
# ---------------------------------------------------------------------------


class TestParseFqnFromGoal:
    @pytest.mark.parametrize(
        ("goal", "expected"),
        [
            (
                "Daily ingest of Iowa liquor sales into ANALYTICS.SALES.IOWA_LIQUOR",
                Destination(database="ANALYTICS", schema="SALES", table="IOWA_LIQUOR"),
            ),
            (
                "ingest the iowa data and load into sales.iowa_sales daily",
                Destination(schema="sales", table="iowa_sales"),
            ),
            (
                "ingest into iowa_sales",
                Destination(table="iowa_sales"),
            ),
            (
                "load to table sales.iowa_sales",
                Destination(schema="sales", table="iowa_sales"),
            ),
            (
                "destination: ANALYTICS.SALES.IOWA_LIQUOR_2024",
                Destination(
                    database="ANALYTICS",
                    schema="SALES",
                    table="IOWA_LIQUOR_2024",
                ),
            ),
        ],
    )
    def test_recognises_destination_phrases(self, goal: str, expected: Destination) -> None:
        assert parse_fqn_from_goal(goal) == expected

    @pytest.mark.parametrize(
        "goal",
        [
            "ingest the daily Iowa liquor sales feed",
            # URL — must not false-match.
            "scrape data from https://data.iowa.gov/resource/m3tr-qhgy.csv",
            # No destination phrase — agent will pick.
            "build a pipeline that does the iowa thing",
            # Empty string.
            "",
        ],
    )
    def test_returns_none_when_no_phrase_matches(self, goal: str) -> None:
        assert parse_fqn_from_goal(goal) is None


# ---------------------------------------------------------------------------
# write_destination_toml — override-vs-inherit logic
# ---------------------------------------------------------------------------


class TestWriteDestinationToml:
    def test_table_only_no_overrides(self, tmp_path: Path) -> None:
        """When db/schema match env defaults, both are commented out."""
        path = tmp_path / "iowa" / "destination.toml"
        write_destination_toml(
            path,
            Destination(table="IOWA_SALES"),
            target="dev",
            env_database="DEV_DB",
            env_schema="RAW",
        )
        content = path.read_text(encoding="utf-8")
        assert 'table = "IOWA_SALES"' in content
        # Both fields show their inherited value, commented.
        assert '# database = "DEV_DB"' in content
        assert '# schema = "RAW"' in content
        # No live override lines (count by-line so we don't false-match
        # the commented-out form).
        live_db = [line for line in content.splitlines() if line.startswith("database =")]
        live_schema = [line for line in content.splitlines() if line.startswith("schema =")]
        assert live_db == []
        assert live_schema == []

    def test_schema_override_only(self, tmp_path: Path) -> None:
        """schema=CURATED differs from env=RAW → schema is a live override."""
        path = tmp_path / "iowa" / "destination.toml"
        write_destination_toml(
            path,
            Destination(table="IOWA_SALES", schema="CURATED"),
            target="dev",
            env_database="DEV_DB",
            env_schema="RAW",
        )
        content = path.read_text(encoding="utf-8")
        assert 'table = "IOWA_SALES"' in content
        # schema is a live override.
        live_schema = [line for line in content.splitlines() if line.startswith("schema =")]
        assert live_schema == ['schema = "CURATED"']
        # database stays commented (matches env).
        live_db = [line for line in content.splitlines() if line.startswith("database =")]
        assert live_db == []

    def test_database_and_schema_override(self, tmp_path: Path) -> None:
        path = tmp_path / "iowa" / "destination.toml"
        write_destination_toml(
            path,
            Destination(table="IOWA", database="ANALYTICS", schema="CURATED"),
            target="dev",
            env_database="DEV_DB",
            env_schema="RAW",
        )
        content = path.read_text(encoding="utf-8")
        # Both live as overrides.
        assert 'database = "ANALYTICS"' in content
        assert 'schema = "CURATED"' in content
        # Both have explanatory comments mentioning the env default
        # they're overriding.
        assert "DEV_DB" in content  # in the comment
        assert "RAW" in content

    def test_value_matches_env_default_left_commented(self, tmp_path: Path) -> None:
        """Defensive: when destination.database == env_database, the
        line is rendered as a commented-out demonstration rather than
        a live override (live and commented are semantically equivalent
        here, but commented makes future env-default changes inherit
        cleanly)."""
        path = tmp_path / "iowa" / "destination.toml"
        write_destination_toml(
            path,
            Destination(table="IOWA", database="DEV_DB"),
            target="dev",
            env_database="DEV_DB",
            env_schema="RAW",
        )
        content = path.read_text(encoding="utf-8")
        live_db = [line for line in content.splitlines() if line.startswith("database =")]
        assert live_db == []  # not promoted to live line
        assert '# database = "DEV_DB"' in content

    def test_no_env_default_treats_value_as_override(self, tmp_path: Path) -> None:
        """If the target has no env_database (e.g. unset env var), any
        destination.database is a live override."""
        path = tmp_path / "iowa" / "destination.toml"
        write_destination_toml(
            path,
            Destination(table="IOWA", database="ANY_DB"),
            target="dev",
            env_database=None,
            env_schema=None,
        )
        content = path.read_text(encoding="utf-8")
        assert 'database = "ANY_DB"' in content


# ---------------------------------------------------------------------------
# read_destination_toml — round-trip + malformed
# ---------------------------------------------------------------------------


class TestReadDestinationToml:
    def test_round_trip_table_only(self, tmp_path: Path) -> None:
        path = tmp_path / "destination.toml"
        write_destination_toml(
            path,
            Destination(table="IOWA"),
            target="dev",
            env_database="DEV_DB",
            env_schema="RAW",
        )
        loaded = read_destination_toml(path)
        assert loaded == Destination(table="IOWA")

    def test_round_trip_with_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "destination.toml"
        write_destination_toml(
            path,
            Destination(table="IOWA", schema="CURATED"),
            target="dev",
            env_database="DEV_DB",
            env_schema="RAW",
        )
        loaded = read_destination_toml(path)
        assert loaded == Destination(table="IOWA", schema="CURATED")

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert read_destination_toml(tmp_path / "absent.toml") is None

    def test_missing_table_field_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "destination.toml"
        path.write_text('database = "x"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="table"):
            read_destination_toml(path)


# ---------------------------------------------------------------------------
# resolve_at_runtime
# ---------------------------------------------------------------------------


class TestResolveAtRuntime:
    def test_no_overrides_uses_env(self) -> None:
        env = {
            "DEV_SNOWFLAKE_DATABASE": "DEV_DB",
            "DEV_SNOWFLAKE_SCHEMA": "RAW",
        }
        result = resolve_at_runtime(Destination(table="IOWA"), env=env, target="dev")
        assert result == ("DEV_DB", "RAW", "IOWA")

    def test_schema_override_wins_over_env(self) -> None:
        env = {
            "DEV_SNOWFLAKE_DATABASE": "DEV_DB",
            "DEV_SNOWFLAKE_SCHEMA": "RAW",
        }
        result = resolve_at_runtime(
            Destination(table="IOWA", schema="CURATED"),
            env=env,
            target="dev",
        )
        assert result == ("DEV_DB", "CURATED", "IOWA")

    def test_missing_env_var_raises(self) -> None:
        """If a field has no override AND the env var is missing, raise.

        This is the loud-failure mode — we'd rather KeyError at startup
        than silently land rows in the wrong place.
        """
        env = {"DEV_SNOWFLAKE_DATABASE": "DEV_DB"}  # no SCHEMA
        with pytest.raises(KeyError, match="DEV_SNOWFLAKE_SCHEMA"):
            resolve_at_runtime(Destination(table="IOWA"), env=env, target="dev")

    def test_target_uppercased_for_env_lookup(self) -> None:
        env = {
            "PROD_SNOWFLAKE_DATABASE": "PROD_DB",
            "PROD_SNOWFLAKE_SCHEMA": "ANALYTICS_RAW",
        }
        # `target` lowercase; resolve uppercases for env lookup.
        result = resolve_at_runtime(Destination(table="IOWA"), env=env, target="prod")
        assert result == ("PROD_DB", "ANALYTICS_RAW", "IOWA")
