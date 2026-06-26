"""Unit tests for the dbt convention-inference engine + the ``dbt_conventions`` Tool.

The spec's "Unit (brownfield style)" slice: over a fixture dbt project the
inference detects `stg_`/`mart_`/`dim_` naming, the staging/marts layout, per-layer
materialization defaults, and generic-test patterns; `render_conventions_md`
produces loader-readable markdown; and `make_dbt_conventions_tool` returns it
offline. Plus the partial-conventions robustness case and the naming-violation
check (the substrate behind dbt-qa's "a model violating the inferred naming
convention is flagged").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.tools import ToolExecutionError
from carve.core.config.paths import ProjectPaths
from carve.core.memory.loader import MemoryLoader
from carve.integrations.dbt.conventions import (
    InferredConventions,
    check_naming_violation,
    conventions_to_dict,
    infer_conventions,
    make_dbt_conventions_tool,
    render_conventions_md,
)

_BROWNFIELD = Path(__file__).parent / "fixtures" / "brownfield_project"


# ---------------------------------------------------------------------------
# Inference over the full brownfield fixture (compiled — manifest present)
# ---------------------------------------------------------------------------


def test_infers_project_name_and_model_count() -> None:
    conv = infer_conventions(_BROWNFIELD)
    assert conv.project_name == "shop"
    # Four models: 2 staging + 2 marts (manifest union raw tree, deduped).
    assert conv.model_count == 4
    assert conv.has_any is True


def test_infers_staging_naming_layout_and_materialization() -> None:
    conv = infer_conventions(_BROWNFIELD)
    staging = conv.layer("staging")
    assert staging.prefixes == ("stg_",)
    assert staging.folder == "staging"
    assert staging.materialization == "view"
    assert staging.model_count == 2


def test_infers_marts_naming_layout_and_materialization() -> None:
    conv = infer_conventions(_BROWNFIELD)
    marts = conv.layer("marts")
    # Both mart prefixes present; most-common-first ordering (1 each → stable set).
    assert set(marts.prefixes) == {"mart_", "dim_"}
    assert marts.folder == "marts"
    assert marts.materialization == "table"
    assert marts.model_count == 2


def test_infers_test_patterns_and_source_freshness() -> None:
    conv = infer_conventions(_BROWNFIELD)
    tests = conv.tests
    assert set(tests.generic_tests) == {"not_null", "unique", "relationships"}
    assert tests.has_source_freshness is True
    # order_id + customer_id both carry unique/not_null somewhere.
    assert set(tests.key_columns_tested) == {"order_id", "customer_id"}


def test_intermediate_layer_absent_when_unused() -> None:
    conv = infer_conventions(_BROWNFIELD)
    intermediate = conv.layer("intermediate")
    assert intermediate.present is False
    assert intermediate.prefixes == ()
    assert intermediate.model_count == 0


# ---------------------------------------------------------------------------
# Pre-compile inference (no target/manifest.json — raw tree + project yml only)
# ---------------------------------------------------------------------------


def test_inference_works_pre_compile(tmp_path: Path) -> None:
    """A project with no compiled manifest still infers from the raw tree + yml.

    Materialization defaults come from dbt_project.yml's models: block (the
    manifest is absent), and naming/layout come from the .sql paths.
    """
    root = tmp_path / "proj"
    (root / "models" / "staging").mkdir(parents=True)
    (root / "models" / "marts").mkdir(parents=True)
    (root / "dbt_project.yml").write_text(
        "name: acme\n"
        "models:\n"
        "  acme:\n"
        "    staging:\n"
        "      +materialized: view\n"
        "    marts:\n"
        "      +materialized: table\n",
        encoding="utf-8",
    )
    (root / "models" / "staging" / "stg_users.sql").write_text("select 1", encoding="utf-8")
    (root / "models" / "marts" / "mart_users.sql").write_text("select 1", encoding="utf-8")

    conv = infer_conventions(root)
    assert conv.project_name == "acme"
    assert conv.model_count == 2
    assert conv.layer("staging").prefixes == ("stg_",)
    assert conv.layer("staging").materialization == "view"  # from dbt_project.yml
    assert conv.layer("marts").prefixes == ("mart_",)
    assert conv.layer("marts").materialization == "table"
    # No compiled tests → empty test section, not a fabricated one.
    assert conv.tests.generic_tests == ()


# ---------------------------------------------------------------------------
# Partial conventions: infer only what's present (no fabrication)
# ---------------------------------------------------------------------------


def test_partial_project_infers_only_what_is_present(tmp_path: Path) -> None:
    """A staging-only project with no tests/marts infers exactly staging."""
    root = tmp_path / "partial"
    (root / "models" / "staging").mkdir(parents=True)
    (root / "dbt_project.yml").write_text("name: tiny\n", encoding="utf-8")
    (root / "models" / "staging" / "stg_thing.sql").write_text("select 1", encoding="utf-8")

    conv = infer_conventions(root)
    assert conv.has_any is True
    assert conv.layer("staging").prefixes == ("stg_",)
    assert conv.layer("staging").folder == "staging"
    # No marts layer, no intermediate, no tests — nothing fabricated.
    assert conv.layer("marts").present is False
    assert conv.layer("intermediate").present is False
    assert conv.tests.generic_tests == ()
    assert conv.tests.has_source_freshness is False
    # No materialization default anywhere → None (not a guessed "view").
    assert conv.layer("staging").materialization is None


def test_empty_project_infers_nothing(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    (root / "dbt_project.yml").write_text("name: blank\n", encoding="utf-8")
    conv = infer_conventions(root)
    assert conv.has_any is False
    assert conv.model_count == 0


def test_missing_project_dir_is_empty_not_crash(tmp_path: Path) -> None:
    conv = infer_conventions(tmp_path / "does_not_exist")
    assert conv.has_any is False


def test_malformed_manifest_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    (root / "target").mkdir(parents=True)
    (root / "dbt_project.yml").write_text("name: broken\n", encoding="utf-8")
    (root / "target" / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ToolExecutionError):
        infer_conventions(root)


# ---------------------------------------------------------------------------
# render_conventions_md → loader-readable markdown
# ---------------------------------------------------------------------------


def test_render_produces_loader_readable_markdown(tmp_path: Path) -> None:
    """The rendered markdown round-trips through MemoryLoader.load_conventions."""
    conv = infer_conventions(_BROWNFIELD)
    md = render_conventions_md(conv)

    # Marks the file as Carve-inferred (not the comment-only init placeholder).
    assert "Inferred project conventions" in md
    assert "`stg_`" in md
    assert "`staging`" in md or "models/staging/" in md
    assert "view" in md
    assert "table" in md
    assert "not_null" in md or "unique" in md

    # Write where the loader reads it; assert it loads as non-empty content.
    carve = tmp_path / "carve"
    carve.mkdir()
    (carve / "conventions.md").write_text(md, encoding="utf-8")
    loader = MemoryLoader(ProjectPaths.from_root(tmp_path))
    loaded = loader.load_conventions()
    assert loaded is not None
    assert loaded.contents == md
    assert loaded.size_bytes > 0


def test_render_empty_project_is_nonempty_but_says_none(tmp_path: Path) -> None:
    """Even an empty project renders a non-empty body (never a false 'inferred')."""
    md = render_conventions_md(InferredConventions())
    assert md.strip() != ""
    assert "No dbt conventions detected" in md


# ---------------------------------------------------------------------------
# The callable Tool (offline, via injected dbt_root)
# ---------------------------------------------------------------------------


def test_tool_infer_returns_structured_record() -> None:
    tool = make_dbt_conventions_tool(dbt_root=_BROWNFIELD)
    assert tool.name == "dbt_conventions"
    out = tool.executor({"op": "infer"})
    assert out["present"] is True
    conv = out["conventions"]
    assert conv["project_name"] == "shop"
    assert conv["layers"]["staging"]["prefixes"] == ["stg_"]
    assert conv["layers"]["staging"]["materialization"] == "view"
    assert conv["layers"]["marts"]["materialization"] == "table"


def test_tool_summary_returns_markdown() -> None:
    tool = make_dbt_conventions_tool(dbt_root=_BROWNFIELD)
    out = tool.executor({"op": "summary"})
    assert out["present"] is True
    assert "Inferred project conventions" in out["markdown"]


def test_tool_missing_project_via_paths_is_empty(tmp_path: Path) -> None:
    """Through ProjectPaths with no dbt project → empty record, not an error."""
    tool = make_dbt_conventions_tool(paths=ProjectPaths.from_root(tmp_path))
    out = tool.executor({"op": "infer"})
    assert out["present"] is False


def test_tool_unknown_op_raises() -> None:
    tool = make_dbt_conventions_tool(dbt_root=_BROWNFIELD)
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "bogus"})


def test_tool_requires_exactly_one_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_dbt_conventions_tool()
    with pytest.raises(ValueError):
        make_dbt_conventions_tool(paths=ProjectPaths.from_root(tmp_path), dbt_root=tmp_path)


def test_tool_name_equals_grant_name() -> None:
    """The binder precondition: Tool.name == the default grant name."""
    tool = make_dbt_conventions_tool(dbt_root=_BROWNFIELD, name="dbt_conventions")
    assert tool.name == "dbt_conventions"


# ---------------------------------------------------------------------------
# check_naming_violation — the dbt-qa substrate
# ---------------------------------------------------------------------------


def _shop_conventions() -> InferredConventions:
    return infer_conventions(_BROWNFIELD)


def test_naming_violation_flags_misnamed_mart() -> None:
    """A mart not prefixed mart_/fct_/dim_ is flagged against the inferred record."""
    conv = _shop_conventions()
    violation = check_naming_violation(
        "revenue_summary",
        path="models/marts/revenue_summary.sql",
        conventions=conv,
    )
    assert violation is not None
    assert violation.layer == "marts"
    assert set(violation.expected_prefixes) == {"mart_", "dim_"}
    assert "revenue_summary" in violation.message


def test_naming_violation_passes_conforming_mart() -> None:
    conv = _shop_conventions()
    assert (
        check_naming_violation(
            "mart_new_thing", path="models/marts/mart_new_thing.sql", conventions=conv
        )
        is None
    )
    assert (
        check_naming_violation(
            "dim_products", path="models/marts/dim_products.sql", conventions=conv
        )
        is None
    )


def test_naming_violation_flags_misnamed_staging() -> None:
    conv = _shop_conventions()
    violation = check_naming_violation(
        "orders_cleaned", path="models/staging/orders_cleaned.sql", conventions=conv
    )
    assert violation is not None
    assert violation.layer == "staging"
    assert violation.expected_prefixes == ("stg_",)


def test_no_violation_when_layer_has_no_inferred_convention(tmp_path: Path) -> None:
    """An empty record establishes no convention → nothing is ever flagged."""
    empty = InferredConventions()
    assert (
        check_naming_violation("weird_name", path="models/marts/weird_name.sql", conventions=empty)
        is None
    )


def test_unclassifiable_model_is_not_flagged() -> None:
    """A model in no known layer (no folder, no prefix) can't violate a layer rule."""
    conv = _shop_conventions()
    assert (
        check_naming_violation("utils_helper", path="models/utils_helper.sql", conventions=conv)
        is None
    )


def test_conventions_to_dict_round_trips_shape() -> None:
    conv = _shop_conventions()
    d = conventions_to_dict(conv)
    assert d["project_name"] == "shop"
    assert d["has_any"] is True
    assert "staging" in d["layers"]
    assert "marts" in d["layers"]
    assert d["tests"]["has_source_freshness"] is True
