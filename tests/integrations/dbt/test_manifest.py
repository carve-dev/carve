"""Unit tests for ``dbt_manifest`` (the compiled-manifest reader Tool).

Built on the shipped ``component_locator._detect_dbt_project`` (root +
one-level-down). Tests inject a resolved ``dbt_root``/``target_path`` (or a
``ProjectPaths``) so they run offline against a fixture ``manifest.json`` — no
live dbt project and no live ``dbt`` run.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from carve.core.agents.tools import ToolExecutionError
from carve.core.config.paths import ProjectPaths
from carve.integrations.dbt.manifest import make_dbt_manifest_tool

_FIXTURES = Path(__file__).parent / "fixtures"


def _target_with_manifest(tmp_path: Path) -> Path:
    """A ``target/`` dir seeded with the fixture ``manifest.json``."""
    target = tmp_path / "target"
    target.mkdir()
    shutil.copy(_FIXTURES / "manifest.json", target / "manifest.json")
    return target


# --- list_models -----------------------------------------------------------


def test_list_models_returns_every_model_with_metadata(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(target_path=_target_with_manifest(tmp_path))
    out = tool.executor({"op": "list_models"})

    by_name = {m["name"]: m for m in out["models"]}
    assert set(by_name) == {"stg_orders", "dim_orders"}
    assert by_name["stg_orders"]["materialized"] == "view"
    assert by_name["stg_orders"]["schema"] == "analytics_staging"
    assert by_name["stg_orders"]["tags"] == ["staging"]
    assert by_name["dim_orders"]["materialized"] == "table"
    # tests / sources are not models
    assert all("test." not in m["unique_id"] for m in out["models"])


def test_list_models_resolves_via_dbt_root(tmp_path: Path) -> None:
    _target_with_manifest(tmp_path)  # writes tmp_path/target/manifest.json
    tool = make_dbt_manifest_tool(dbt_root=tmp_path)
    names = {m["name"] for m in tool.executor({"op": "list_models"})["models"]}
    assert names == {"stg_orders", "dim_orders"}


def test_list_models_detects_project_one_level_down_via_paths(tmp_path: Path) -> None:
    for sub in ("el", "pipelines", "carve", ".carve", ".dlt"):
        (tmp_path / sub).mkdir()
    dbt_root = tmp_path / "analytics"
    dbt_root.mkdir()
    (dbt_root / "dbt_project.yml").write_text("name: analytics\n")
    _target_with_manifest(dbt_root)

    tool = make_dbt_manifest_tool(paths=ProjectPaths.from_root(tmp_path))
    names = {m["name"] for m in tool.executor({"op": "list_models"})["models"]}
    assert names == {"stg_orders", "dim_orders"}


# --- model_columns ---------------------------------------------------------


def test_model_columns_returns_declared_columns(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(target_path=_target_with_manifest(tmp_path))
    out = tool.executor({"op": "model_columns", "model": "stg_orders"})

    assert out["found"] is True
    by_name = {c["name"]: c for c in out["columns"]}
    assert set(by_name) == {"order_id", "customer_id"}
    assert by_name["order_id"]["description"] == "Primary key for the order."
    assert by_name["order_id"]["data_type"] == "integer"
    # column-attached tests surface against the column
    assert sorted(by_name["order_id"]["tests"]) == ["not_null", "unique"]
    assert by_name["customer_id"]["tests"] == []


def test_model_columns_accepts_full_unique_id(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(target_path=_target_with_manifest(tmp_path))
    out = tool.executor({"op": "model_columns", "model": "model.analytics.stg_orders"})
    assert out["found"] is True
    assert {c["name"] for c in out["columns"]} == {"order_id", "customer_id"}


def test_model_columns_for_unknown_model_is_not_found(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(target_path=_target_with_manifest(tmp_path))
    out = tool.executor({"op": "model_columns", "model": "nope"})
    assert out == {"found": False, "model": "nope"}


# --- model_dependencies ----------------------------------------------------


def test_model_dependencies_resolves_upstream_and_downstream(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(target_path=_target_with_manifest(tmp_path))
    out = tool.executor({"op": "model_dependencies", "model": "stg_orders"})

    assert out["found"] is True
    upstream = {d["name"]: d for d in out["upstream"]}
    assert upstream["orders"]["resource_type"] == "source"
    downstream = {d["name"]: d for d in out["downstream"]}
    # dim_orders is downstream; the attached tests are NOT downstream deps
    assert set(downstream) == {"dim_orders"}
    assert downstream["dim_orders"]["resource_type"] == "model"


def test_model_dependencies_for_leaf_model(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(target_path=_target_with_manifest(tmp_path))
    out = tool.executor({"op": "model_dependencies", "model": "dim_orders"})
    assert {d["name"] for d in out["upstream"]} == {"stg_orders"}
    assert out["downstream"] == []


# --- tests_on_model --------------------------------------------------------


def test_tests_on_model_returns_attached_tests(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(target_path=_target_with_manifest(tmp_path))
    out = tool.executor({"op": "tests_on_model", "model": "stg_orders"})

    assert out["found"] is True
    by_kind = {t["kind"]: t for t in out["tests"]}
    assert set(by_kind) == {"not_null", "unique"}
    assert by_kind["not_null"]["column"] == "order_id"
    assert by_kind["unique"]["column"] == "order_id"


def test_tests_on_model_for_model_without_tests(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(target_path=_target_with_manifest(tmp_path))
    out = tool.executor({"op": "tests_on_model", "model": "dim_orders"})
    assert out["found"] is True
    assert out["tests"] == []


# --- missing project / malformed / arg validation --------------------------


def test_no_dbt_project_returns_empty_models(tmp_path: Path) -> None:
    for sub in ("el", "pipelines", "carve", ".carve", ".dlt"):
        (tmp_path / sub).mkdir()
    tool = make_dbt_manifest_tool(paths=ProjectPaths.from_root(tmp_path))
    assert tool.executor({"op": "list_models"}) == {"models": []}


def test_missing_manifest_returns_empty_not_found(tmp_path: Path) -> None:
    # dbt_root exists but no target/manifest.json (project never compiled).
    tool = make_dbt_manifest_tool(dbt_root=tmp_path)
    assert tool.executor({"op": "list_models"}) == {"models": []}
    assert tool.executor({"op": "model_columns", "model": "stg_orders"}) == {
        "found": False,
        "model": "stg_orders",
    }


def test_malformed_manifest_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text("{not valid json", encoding="utf-8")
    tool = make_dbt_manifest_tool(target_path=target)
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "list_models"})


def test_per_model_op_requires_model(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(target_path=_target_with_manifest(tmp_path))
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "model_columns"})


def test_unknown_op_errors(tmp_path: Path) -> None:
    tool = make_dbt_manifest_tool(dbt_root=tmp_path)
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "frobnicate"})


def test_factory_requires_exactly_one_resolution_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_dbt_manifest_tool()
    with pytest.raises(ValueError):
        make_dbt_manifest_tool(dbt_root=tmp_path, paths=ProjectPaths.from_root(tmp_path))


def test_tool_name_equals_grant_name(tmp_path: Path) -> None:
    # Binder precondition: injected.name must equal the grant name.
    tool = make_dbt_manifest_tool(dbt_root=tmp_path)
    assert tool.name == "dbt_manifest"
