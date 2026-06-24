"""Engine resolution + pin write-back + reuse-not-re-resolve."""

from __future__ import annotations

import textwrap
from pathlib import Path

import tomlkit

from carve.core.config.schema import ComponentConfig, ComponentMode, ComponentType
from carve.core.dbt_execution.engine import (
    ENGINE_DBT_CORE,
    ENGINE_FUSION,
    EnginePin,
    pin_engine,
    resolve_engine,
    resolve_or_reuse,
)


def test_fusion_dialects_resolve_to_fusion() -> None:
    for dialect in ("snowflake", "bigquery", "databricks", "redshift"):
        pin = resolve_engine(dialect)
        assert pin.dbt_engine == ENGINE_FUSION
        assert pin.dbt_version  # a default version is recorded


def test_duckdb_and_postgres_fall_back_to_dbt_core() -> None:
    for dialect in ("duckdb", "postgres", "sqlite", "trino"):
        assert resolve_engine(dialect).dbt_engine == ENGINE_DBT_CORE


def test_resolve_engine_is_case_insensitive() -> None:
    assert resolve_engine("Snowflake").dbt_engine == ENGINE_FUSION
    assert resolve_engine("  DuckDB ").dbt_engine == ENGINE_DBT_CORE


def test_pin_engine_writes_back_preserving_comments(tmp_path: Path) -> None:
    config_path = tmp_path / "carve.toml"
    config_path.write_text(
        textwrap.dedent("""
        # Carve project config
        [project]
        name = "demo"

        [components.warehouse]  # the dbt component
        type = "dbt"
        mode = "same-repo"
        """),
        encoding="utf-8",
    )

    pin_engine(
        "warehouse",
        EnginePin(dbt_engine=ENGINE_FUSION, dbt_version="2.0.0"),
        config_path=config_path,
    )

    text = config_path.read_text(encoding="utf-8")
    assert "# Carve project config" in text
    assert "# the dbt component" in text

    doc = tomlkit.parse(text)
    block = doc["components"]["warehouse"]
    assert block["dbt_engine"] == ENGINE_FUSION
    assert block["dbt_version"] == "2.0.0"


def test_pin_engine_missing_block_raises(tmp_path: Path) -> None:
    config_path = tmp_path / "carve.toml"
    config_path.write_text('[project]\nname = "demo"\n', encoding="utf-8")
    try:
        pin_engine(
            "absent",
            EnginePin(dbt_engine=ENGINE_DBT_CORE, dbt_version="1.8.0"),
            config_path=config_path,
        )
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected KeyError for a missing component block")


def test_resolve_or_reuse_reuses_existing_pin(monkeypatch) -> None:
    component = ComponentConfig(
        type=ComponentType.DBT,
        mode=ComponentMode.SAME_REPO,
        dbt_engine="dbt-core",
        dbt_version="1.7.3",
    )

    # If reuse short-circuits, resolve_engine must never be called.
    called = False

    def _boom(_dialect: str) -> EnginePin:  # pragma: no cover - asserted not called
        nonlocal called
        called = True
        raise AssertionError("resolve_engine should not run when a pin exists")

    monkeypatch.setattr("carve.core.dbt_execution.engine.resolve_engine", _boom)
    pin = resolve_or_reuse(component, "snowflake")

    assert called is False
    assert pin.dbt_engine == "dbt-core"
    assert pin.dbt_version == "1.7.3"


def test_resolve_or_reuse_resolves_when_unpinned() -> None:
    component = ComponentConfig(type=ComponentType.DBT, mode=ComponentMode.SAME_REPO)
    pin = resolve_or_reuse(component, "snowflake")
    assert pin.dbt_engine == ENGINE_FUSION
