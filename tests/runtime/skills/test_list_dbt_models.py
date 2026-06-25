"""Tests for the ``list_dbt_models`` alias Tool.

``list_dbt_models`` is a thin alias over the shipped
``make_dbt_manifest_tool`` op=``list_models`` — it must return the manifest's
models (proving the alias delegates rather than re-parsing) while exposing a
no-``op``-input surface, and its ``Tool.name`` must equal the grant name.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from carve.core.config.paths import ProjectPaths
from carve.runtime.skills.list_dbt_models import make_list_dbt_models_tool

# Reuse the shipped dbt-manifest fixture (models: stg_orders, dim_orders).
_MANIFEST_FIXTURE = (
    Path(__file__).resolve().parents[2] / "integrations" / "dbt" / "fixtures" / "manifest.json"
)


def _target_with_manifest(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    shutil.copy(_MANIFEST_FIXTURE, target / "manifest.json")
    return target


def test_aliases_manifest_list_models(tmp_path: Path) -> None:
    # The wrapper pins op=list_models on the shipped reader: it returns the
    # manifest's models, proving it delegates rather than re-parsing.
    tool = make_list_dbt_models_tool(target_path=_target_with_manifest(tmp_path))
    out = tool.executor({})
    names = {m["name"] for m in out["models"]}
    assert names == {"stg_orders", "dim_orders"}
    by_name = {m["name"]: m for m in out["models"]}
    assert by_name["stg_orders"]["materialized"] == "view"
    assert by_name["dim_orders"]["materialized"] == "table"


def test_empty_models_when_no_manifest(tmp_path: Path) -> None:
    # A missing manifest yields an empty result, not a crash (the shipped
    # reader's fail-open behavior flows through the alias).
    paths = ProjectPaths.from_root(tmp_path)
    out = make_list_dbt_models_tool(paths=paths).executor({})
    assert out["models"] == []


def test_resolves_via_dbt_root(tmp_path: Path) -> None:
    _target_with_manifest(tmp_path)  # writes tmp_path/target/manifest.json
    out = make_list_dbt_models_tool(dbt_root=tmp_path).executor({})
    assert {m["name"] for m in out["models"]} == {"stg_orders", "dim_orders"}


def test_tool_name_equals_grant_name(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)
    assert make_list_dbt_models_tool(paths=paths).name == "list_dbt_models"
    assert make_list_dbt_models_tool(paths=paths, name="custom").name == "custom"
