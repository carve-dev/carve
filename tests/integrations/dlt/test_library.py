"""Unit tests for ``dlt_library`` (list / lookup / copy over the curated corpus).

Uses a tmp_path corpus fixture for deterministic list/lookup/copy assertions and
also exercises the real shipped ``src/carve/sources`` corpus to prove the
reference pack is discoverable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.tools import ToolExecutionError
from carve.core.skills.packs import load_skill_pack
from carve.integrations.dlt.library import make_dlt_library_tool
from carve.integrations.provenance import parse_provenance_header

REAL_SOURCES_DIR = Path(__file__).resolve().parents[3] / "src" / "carve" / "sources"

_HN_SKILL = """\
---
name: _reference_hackernews
description: Hacker News API source for dlt. Loads stories and comments.
supported_destinations:
  - duckdb
  - snowflake
last_updated: "2026-06-23"
---
Reference HN pack body.
"""

_HN_SOURCE = """\
import dlt

DESTINATION = "__DESTINATION__"
SCHEMA = "__SCHEMA__"


@dlt.source
def hacker_news():
    return []
"""

_OTHER_SKILL = """\
---
name: widgets
description: A widget warehouse connector unrelated to news.
---
Widgets body.
"""


def _make_corpus(root: Path) -> Path:
    sources = root / "sources"
    hn = sources / "_reference_hackernews"
    (hn / "scripts").mkdir(parents=True)
    (hn / "SKILL.md").write_text(_HN_SKILL)
    (hn / "scripts" / "__init__.py").write_text(_HN_SOURCE)
    (hn / "scripts" / "requirements.txt").write_text("dlt[duckdb]==1.28.1\n")

    other = sources / "widgets"
    (other / "scripts").mkdir(parents=True)
    (other / "SKILL.md").write_text(_OTHER_SKILL)
    (other / "scripts" / "__init__.py").write_text("import dlt\n")
    return sources


def _project(root: Path) -> Path:
    (root / "el").mkdir(parents=True)
    return root


# --- list ------------------------------------------------------------------


def test_list_enumerates_packs_with_metadata(tmp_path: Path) -> None:
    sources = _make_corpus(tmp_path)
    tool = make_dlt_library_tool(sources, project_dir=_project(tmp_path / "proj"))
    out = tool.executor({"op": "list"})
    by_name = {p["name"]: p for p in out["packs"]}
    assert set(by_name) == {"_reference_hackernews", "widgets"}
    hn = by_name["_reference_hackernews"]
    assert "Hacker News" in hn["description"]
    assert hn["supported_destinations"] == ["duckdb", "snowflake"]
    assert hn["last_updated"] == "2026-06-23"


def test_list_finds_the_real_reference_pack() -> None:
    tool = make_dlt_library_tool(REAL_SOURCES_DIR, project_dir=Path("/nonexistent-proj"))
    names = {p["name"] for p in tool.executor({"op": "list"})["packs"]}
    assert "_reference_hackernews" in names


# --- lookup ----------------------------------------------------------------


def test_lookup_returns_hn_with_high_confidence(tmp_path: Path) -> None:
    sources = _make_corpus(tmp_path)
    tool = make_dlt_library_tool(sources, project_dir=_project(tmp_path / "proj"))
    out = tool.executor({"op": "lookup", "query": "hacker news"})
    names = [m["name"] for m in out["matches"]]
    assert "_reference_hackernews" in names
    hn = next(m for m in out["matches"] if m["name"] == "_reference_hackernews")
    # "hacker news" hits the description -> at least medium; banding is non-low.
    assert hn["confidence"] in ("high", "medium")


def test_lookup_name_substring_is_high_confidence(tmp_path: Path) -> None:
    sources = _make_corpus(tmp_path)
    tool = make_dlt_library_tool(sources, project_dir=_project(tmp_path / "proj"))
    out = tool.executor({"op": "lookup", "query": "widgets"})
    widgets = next(m for m in out["matches"] if m["name"] == "widgets")
    assert widgets["confidence"] == "high"


def test_lookup_unrelated_query_no_high_confidence(tmp_path: Path) -> None:
    sources = _make_corpus(tmp_path)
    tool = make_dlt_library_tool(sources, project_dir=_project(tmp_path / "proj"))
    out = tool.executor({"op": "lookup", "query": "salesforce crm pipeline"})
    assert not any(m["confidence"] == "high" for m in out["matches"])


def test_lookup_empty_query_errors(tmp_path: Path) -> None:
    sources = _make_corpus(tmp_path)
    tool = make_dlt_library_tool(sources, project_dir=_project(tmp_path / "proj"))
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "lookup", "query": "   "})


# --- copy ------------------------------------------------------------------


def test_copy_lays_source_customizes_and_stamps_provenance(tmp_path: Path) -> None:
    sources = _make_corpus(tmp_path)
    proj = _project(tmp_path / "proj")
    tool = make_dlt_library_tool(sources, project_dir=proj, library_commit="abc1234")

    out = tool.executor(
        {
            "op": "copy",
            "name": "_reference_hackernews",
            "dest_path": "el/hn",
            "customization": {"destination": "snowflake", "schema": "raw_hn"},
        }
    )

    assert out["library_name"] == "_reference_hackernews"
    assert out["library_commit"] == "abc1234"
    written = proj / "el" / "hn" / "__init__.py"
    assert written.is_file()
    body = written.read_text()
    # Customization substituted.
    assert 'DESTINATION = "snowflake"' in body
    assert 'SCHEMA = "raw_hn"' in body
    assert "__DESTINATION__" not in body
    # Provenance header round-trips library name + commit.
    header = parse_provenance_header(body)
    assert header is not None
    assert header.source == "carve/sources/_reference_hackernews"
    assert header.commit == "abc1234"
    assert header.destination == "snowflake"
    # requirements.txt copied verbatim (no header, no substitution).
    reqs = (proj / "el" / "hn" / "requirements.txt").read_text()
    assert reqs == "dlt[duckdb]==1.28.1\n"
    # files_written lists both files.
    assert any(f.endswith("__init__.py") for f in out["files_written"])
    assert any(f.endswith("requirements.txt") for f in out["files_written"])


def test_copy_unknown_pack_raises(tmp_path: Path) -> None:
    sources = _make_corpus(tmp_path)
    proj = _project(tmp_path / "proj")
    tool = make_dlt_library_tool(sources, project_dir=proj, library_commit="abc1234")
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "copy", "name": "../../etc", "dest_path": "el/x"})


def test_copy_rejects_dest_outside_el(tmp_path: Path) -> None:
    sources = _make_corpus(tmp_path)
    proj = _project(tmp_path / "proj")
    tool = make_dlt_library_tool(sources, project_dir=proj, library_commit="abc1234")
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "copy", "name": "_reference_hackernews", "dest_path": "../escape"})
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "copy", "name": "_reference_hackernews", "dest_path": "el/../../etc"})


def test_copy_requires_name_and_dest(tmp_path: Path) -> None:
    sources = _make_corpus(tmp_path)
    proj = _project(tmp_path / "proj")
    tool = make_dlt_library_tool(sources, project_dir=proj, library_commit="abc1234")
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "copy", "dest_path": "el/hn"})
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "copy", "name": "_reference_hackernews"})


# --- copy is mode-gated (fail-closed below build) --------------------------


def test_copy_denied_below_build(tmp_path: Path) -> None:
    """`copy` writes into el/ and is fail-closed below build (like sql writes).

    The harness gate admits `dlt_library` by name in every mode; the copy
    authority lives in the tool, so a PLAN/READ_ONLY child gets a clear
    ToolExecutionError instead of a write.
    """
    sources = _make_corpus(tmp_path)
    proj = _project(tmp_path / "proj")
    for mode in (PermissionMode.READ_ONLY, PermissionMode.PLAN):
        tool = make_dlt_library_tool(sources, project_dir=proj, library_commit="abc1234", mode=mode)
        with pytest.raises(ToolExecutionError, match="build mode"):
            tool.executor({"op": "copy", "name": "_reference_hackernews", "dest_path": "el/hn"})
        # The write never happened.
        assert not (proj / "el" / "hn").exists()


def test_copy_allowed_at_build(tmp_path: Path) -> None:
    """At build the copy runs and lays the source into el/."""
    sources = _make_corpus(tmp_path)
    proj = _project(tmp_path / "proj")
    tool = make_dlt_library_tool(
        sources, project_dir=proj, library_commit="abc1234", mode=PermissionMode.BUILD
    )
    out = tool.executor({"op": "copy", "name": "_reference_hackernews", "dest_path": "el/hn"})
    assert out["library_name"] == "_reference_hackernews"
    assert (proj / "el" / "hn" / "__init__.py").is_file()


def test_list_and_lookup_run_at_plan(tmp_path: Path) -> None:
    """list/lookup are pure reads — available even at PLAN (read floor)."""
    sources = _make_corpus(tmp_path)
    proj = _project(tmp_path / "proj")
    tool = make_dlt_library_tool(
        sources, project_dir=proj, library_commit="abc1234", mode=PermissionMode.PLAN
    )
    listed = tool.executor({"op": "list"})
    assert {p["name"] for p in listed["packs"]} == {"_reference_hackernews", "widgets"}
    looked = tool.executor({"op": "lookup", "query": "hacker news"})
    assert any(m["name"] == "_reference_hackernews" for m in looked["matches"])


def test_copy_default_mode_is_permissive(tmp_path: Path) -> None:
    """The default mode (no `mode=`) is the most permissive — copy runs.

    Back-compat for the many direct-construction call sites that exercise copy
    without threading a mode; the orchestrator threads the clamped child mode.
    """
    sources = _make_corpus(tmp_path)
    proj = _project(tmp_path / "proj")
    tool = make_dlt_library_tool(sources, project_dir=proj, library_commit="abc1234")
    out = tool.executor({"op": "copy", "name": "_reference_hackernews", "dest_path": "el/hn"})
    assert (proj / "el" / "hn" / "__init__.py").is_file()
    assert out["library_commit"] == "abc1234"


# --- binder precondition + skill-pack validity -----------------------------


def test_tool_name_equals_grant_name(tmp_path: Path) -> None:
    tool = make_dlt_library_tool(REAL_SOURCES_DIR, project_dir=tmp_path)
    assert tool.name == "dlt_library"


def test_unknown_op_errors(tmp_path: Path) -> None:
    tool = make_dlt_library_tool(REAL_SOURCES_DIR, project_dir=tmp_path)
    with pytest.raises(ToolExecutionError):
        tool.executor({"op": "frobnicate"})


def test_reference_pack_loads_as_skill_pack() -> None:
    pack = load_skill_pack(REAL_SOURCES_DIR / "_reference_hackernews")
    assert pack.name == "_reference_hackernews"
    assert "Hacker News" in pack.description
    # Bundled scripts are recorded (not imported).
    script_names = {p.name for p in pack.script_paths}
    assert "__init__.py" in script_names
    assert "requirements.txt" in script_names
