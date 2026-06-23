"""The dlt-engineer's lightweight callable skills (Tools): inspect + REST probe."""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.tools import ToolExecutionError
from carve.integrations.dlt.code_emitter import with_provenance_header
from carve.integrations.dlt.skills import (
    make_existing_dlt_inspect_tool,
    make_rest_api_explore_tool,
)

# --- existing_dlt_inspect --------------------------------------------------


def _component(root: Path, name: str, init: str, *, requirements: str = "dlt>=1.0\n") -> None:
    d = root / "el" / name
    d.mkdir(parents=True)
    (d / "__init__.py").write_text(init)
    (d / "requirements.txt").write_text(requirements)


def test_inspect_list_classifies_provenance(tmp_path: Path) -> None:
    _component(
        tmp_path, "carve_made", with_provenance_header("import dlt\n", source="s", commit="abc1234")
    )
    _component(tmp_path, "hand_made", "import dlt  # written by a human\n")
    tool = make_existing_dlt_inspect_tool(tmp_path)
    out = tool.executor({"op": "list"})
    by = {p["name"]: p["provenance"] for p in out["pipelines"]}
    assert by == {"carve_made": "carve-generated", "hand_made": "user-authored"}


def test_inspect_list_empty_when_no_el(tmp_path: Path) -> None:
    assert make_existing_dlt_inspect_tool(tmp_path).executor({"op": "list"}) == {"pipelines": []}


def test_inspect_read_returns_files(tmp_path: Path) -> None:
    _component(tmp_path, "stripe", "import dlt\n@dlt.source\ndef s(): ...\n")
    out = make_existing_dlt_inspect_tool(tmp_path).executor({"op": "read", "name": "stripe"})
    assert out["name"] == "stripe"
    assert "import dlt" in out["files"]["__init__.py"]
    assert out["files"]["requirements.txt"] == "dlt>=1.0\n"


def test_inspect_read_unknown_component_errors(tmp_path: Path) -> None:
    (tmp_path / "el").mkdir()
    with pytest.raises(ToolExecutionError):
        make_existing_dlt_inspect_tool(tmp_path).executor({"op": "read", "name": "ghost"})


def test_inspect_read_rejects_path_traversal(tmp_path: Path) -> None:
    _component(tmp_path, "ok", "import dlt\n")
    with pytest.raises(ToolExecutionError):
        make_existing_dlt_inspect_tool(tmp_path).executor({"op": "read", "name": "../../etc"})


def test_inspect_unknown_op_errors(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError):
        make_existing_dlt_inspect_tool(tmp_path).executor({"op": "frobnicate"})


# --- rest_api_explore ------------------------------------------------------


def _recording_fetcher(responses: dict[tuple[str, str], tuple[int, str]]):
    calls: list[tuple[str, str]] = []

    def _fetch(url: str, method: str) -> tuple[int, str]:
        calls.append((url, method))
        return responses.get((url, method), (404, ""))

    return _fetch, calls


def test_explore_probes_options_then_schema_then_endpoints() -> None:
    fetch, calls = _recording_fetcher(
        {
            ("https://api.x.com/", "OPTIONS"): (200, "GET, POST"),
            ("https://api.x.com/openapi.json", "GET"): (200, '{"paths": {}}'),
        }
    )
    tool = make_rest_api_explore_tool(fetcher=fetch)
    out = tool.executor({"base_url": "https://api.x.com", "endpoints": ["users"]})
    methods = {(u, m) for u, m in calls}
    assert ("https://api.x.com/", "OPTIONS") in methods
    assert ("https://api.x.com/openapi.json", "GET") in methods
    assert ("https://api.x.com/users", "GET") in methods
    # Only read verbs, ever.
    assert all(m in ("OPTIONS", "GET") for _u, m in calls)
    assert out["requests_made"] == len(calls)


def test_explore_respects_request_cap() -> None:
    fetch, calls = _recording_fetcher({})
    tool = make_rest_api_explore_tool(fetcher=fetch, max_requests=2)
    tool.executor({"base_url": "https://api.x.com", "endpoints": [f"e{i}" for i in range(50)]})
    assert len(calls) == 2


def test_explore_truncates_large_bodies() -> None:
    big = "x" * 100_000
    fetch, _ = _recording_fetcher({("https://api.x.com/", "OPTIONS"): (200, big)})
    tool = make_rest_api_explore_tool(fetcher=fetch, max_body=1000)
    out = tool.executor({"base_url": "https://api.x.com"})
    opts = next(r for r in out["results"] if r["method"] == "OPTIONS")
    assert len(opts["body"]) == 1000
    assert opts["truncated"] is True


def test_explore_rejects_non_http_base_url() -> None:
    with pytest.raises(ToolExecutionError):
        make_rest_api_explore_tool(fetcher=lambda u, m: (200, "")).executor({"base_url": "ftp://x"})
