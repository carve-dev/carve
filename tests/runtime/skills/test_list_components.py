"""Tests for the ``list_components`` callable Tool.

Covers the happy path over a convention fixture (el/<name>/ dirs + the detected
dbt project), the block-defined override (a [components.<name>] block wins over a
same-named convention entry), and the binder precondition
``Tool.name == grant name``.
"""

from __future__ import annotations

from pathlib import Path

from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import ComponentConfig, ComponentMode, ComponentType
from carve.runtime.skills.list_components import make_list_components_tool


def _dlt_component(root: Path, name: str) -> None:
    (root / "el" / name).mkdir(parents=True)


def _dbt_project(root: Path, dirname: str = "analytics") -> None:
    proj = root / dirname
    proj.mkdir()
    (proj / "dbt_project.yml").write_text("name: analytics\n", encoding="utf-8")


def test_lists_convention_dlt_and_dbt_components(tmp_path: Path) -> None:
    _dlt_component(tmp_path, "stripe")
    _dlt_component(tmp_path, "shopify")
    _dbt_project(tmp_path, "analytics")
    paths = ProjectPaths.from_root(tmp_path)

    out = make_list_components_tool(paths=paths).executor({})
    by_name = {c["name"]: c for c in out["components"]}

    assert set(by_name) == {"stripe", "shopify", "analytics"}
    assert by_name["stripe"] == {"name": "stripe", "type": "dlt", "mode": "convention"}
    assert by_name["analytics"]["type"] == "dbt"
    assert by_name["analytics"]["mode"] == "convention"
    # Sorted by name for stable output.
    assert [c["name"] for c in out["components"]] == ["analytics", "shopify", "stripe"]


def test_empty_when_no_components(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)
    assert make_list_components_tool(paths=paths).executor({}) == {"components": []}


def test_block_defined_component_appears_with_its_mode(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)
    blocks = {
        "warehouse_dbt": ComponentConfig(
            type=ComponentType.DBT,
            mode=ComponentMode.SEPARATE_REMOTE,
            url="https://example.com/warehouse.git",
        )
    }
    out = make_list_components_tool(paths=paths, components=blocks).executor({})
    by_name = {c["name"]: c for c in out["components"]}
    assert by_name["warehouse_dbt"] == {
        "name": "warehouse_dbt",
        "type": "dbt",
        "mode": "separate-remote",
    }


def test_block_overrides_same_named_convention_entry(tmp_path: Path) -> None:
    # A graduated component keeps its name but the block carries the explicit
    # mode; the block must win over the convention el/<name>/ entry.
    _dlt_component(tmp_path, "stripe")
    paths = ProjectPaths.from_root(tmp_path)
    blocks = {
        "stripe": ComponentConfig(
            type=ComponentType.DLT,
            mode=ComponentMode.SEPARATE_LOCAL,
            path="/somewhere/stripe",
        )
    }
    out = make_list_components_tool(paths=paths, components=blocks).executor({})
    by_name = {c["name"]: c for c in out["components"]}
    assert by_name["stripe"]["mode"] == "separate-local"
    # Only one entry for the name (no duplicate convention + block row).
    assert sum(1 for c in out["components"] if c["name"] == "stripe") == 1


def test_tool_name_equals_grant_name(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)
    assert make_list_components_tool(paths=paths).name == "list_components"
    assert make_list_components_tool(paths=paths, name="custom").name == "custom"
