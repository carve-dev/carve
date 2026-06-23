"""Unit tests for ``dbt_source_lookup`` (the dbt sources.yml reader Tool).

Built on the shipped ``component_locator._detect_dbt_project`` (root +
one-level-down). Tests inject a resolved ``dbt_root`` (or ``ProjectPaths``) so
they run offline against a fixture dbt project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.tools import ToolExecutionError
from carve.core.config.paths import ProjectPaths
from carve.integrations.dbt.sources import make_dbt_source_lookup_tool


def _write_sources(dbt_root: Path, relpath: str, body: str) -> None:
    target = dbt_root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


_STRIPE_AND_GITHUB = """\
version: 2
sources:
  - name: stripe
    schema: raw_stripe
    tables:
      - name: charges
      - name: customers
  - name: github
    tables:
      - name: issues
"""


def test_list_returns_all_declarations_with_schema_default(tmp_path: Path) -> None:
    _write_sources(tmp_path, "models/staging/_sources.yml", _STRIPE_AND_GITHUB)
    tool = make_dbt_source_lookup_tool(dbt_root=tmp_path)

    out = tool.executor({"op": "list"})
    by_name = {s["name"]: s for s in out["sources"]}

    assert set(by_name) == {"stripe", "github"}
    assert by_name["stripe"]["schema"] == "raw_stripe"
    # schema defaults to the source name when omitted
    assert by_name["github"]["schema"] == "github"
    assert [t["name"] for t in by_name["stripe"]["tables"]] == ["charges", "customers"]


def test_list_detects_project_one_level_down_via_paths(tmp_path: Path) -> None:
    for sub in ("el", "pipelines", "carve", ".carve", ".dlt"):
        (tmp_path / sub).mkdir()
    dbt_root = tmp_path / "analytics"
    dbt_root.mkdir()
    (dbt_root / "dbt_project.yml").write_text("name: analytics\n")
    _write_sources(dbt_root, "models/_sources.yml", _STRIPE_AND_GITHUB)

    tool = make_dbt_source_lookup_tool(paths=ProjectPaths.from_root(tmp_path))
    names = {s["name"] for s in tool.executor({"op": "list"})["sources"]}
    assert names == {"stripe", "github"}


def test_match_returns_source_config_for_declared_schema_table(tmp_path: Path) -> None:
    _write_sources(tmp_path, "_sources.yml", _STRIPE_AND_GITHUB)
    tool = make_dbt_source_lookup_tool(dbt_root=tmp_path)

    out = tool.executor({"op": "match", "schema": "raw_stripe", "table": "charges"})
    assert out["found"] is True
    assert out["source"]["name"] == "stripe"
    assert out["table"]["name"] == "charges"


def test_match_returns_not_found_for_undeclared(tmp_path: Path) -> None:
    _write_sources(tmp_path, "_sources.yml", _STRIPE_AND_GITHUB)
    tool = make_dbt_source_lookup_tool(dbt_root=tmp_path)

    out = tool.executor({"op": "match", "schema": "raw_stripe", "table": "nope"})
    assert out["found"] is False
    assert out["schema"] == "raw_stripe"
    assert out["table"] == "nope"


def test_no_dbt_project_returns_empty_list(tmp_path: Path) -> None:
    for sub in ("el", "pipelines", "carve", ".carve", ".dlt"):
        (tmp_path / sub).mkdir()
    tool = make_dbt_source_lookup_tool(paths=ProjectPaths.from_root(tmp_path))
    assert tool.executor({"op": "list"}) == {"sources": []}


def test_malformed_sources_yaml_fails_closed(tmp_path: Path) -> None:
    _write_sources(tmp_path, "_sources.yml", "sources: [: : not yaml\n  - oops")
    tool = make_dbt_source_lookup_tool(dbt_root=tmp_path)
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "list"})


def test_match_requires_schema_and_table(tmp_path: Path) -> None:
    _write_sources(tmp_path, "_sources.yml", _STRIPE_AND_GITHUB)
    tool = make_dbt_source_lookup_tool(dbt_root=tmp_path)
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "match", "schema": "raw_stripe"})
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "match", "table": "charges"})


def test_unknown_op_errors(tmp_path: Path) -> None:
    tool = make_dbt_source_lookup_tool(dbt_root=tmp_path)
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "frobnicate"})


def test_factory_requires_exactly_one_resolution_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_dbt_source_lookup_tool()
    with pytest.raises(ValueError):
        make_dbt_source_lookup_tool(dbt_root=tmp_path, paths=ProjectPaths.from_root(tmp_path))


def test_tool_name_equals_grant_name(tmp_path: Path) -> None:
    # Binder precondition: injected.name must equal the grant name.
    tool = make_dbt_source_lookup_tool(dbt_root=tmp_path)
    assert tool.name == "dbt_source_lookup"
