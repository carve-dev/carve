"""Tests for the ``pipeline_inspect`` callable Tool.

Covers the happy path over a fixture pipeline (``list`` + ``read``), the
path-confinement / not-found error classes, that a malformed pipeline surfaces
the structured ``load_pipeline`` validation error (the same gate
``carve pipelines validate`` runs), and the binder precondition
``Tool.name == grant name``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.tools import ToolExecutionError
from carve.core.config.paths import ProjectPaths
from carve.runtime.skills.pipeline_inspect import make_pipeline_inspect_tool


def _project(tmp_path: Path) -> ProjectPaths:
    """A project with one el/<name>/ dlt component, ready for pipelines."""
    (tmp_path / "el" / "stripe").mkdir(parents=True)
    (tmp_path / "pipelines").mkdir()
    return ProjectPaths.from_root(tmp_path)


def _write_pipeline(paths: ProjectPaths, name: str, body: str) -> None:
    (paths.pipelines_dir / f"{name}.toml").write_text(body, encoding="utf-8")


_VALID = """\
[pipeline]
description = "ingest stripe then build"

[seed_schedule]
cron = "0 2 * * *"

[[steps]]
id = "ingest"
type = "dlt"
component = "stripe"

[[steps]]
id = "transform"
type = "dlt"
component = "stripe"
depends_on = ["ingest"]
"""


def test_list_returns_pipeline_stems(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    _write_pipeline(paths, "daily", _VALID)
    _write_pipeline(paths, "hourly", _VALID)
    tool = make_pipeline_inspect_tool(paths=paths)
    out = tool.executor({"op": "list"})
    assert out["pipelines"] == ["daily", "hourly"]


def test_list_empty_when_no_pipelines_dir(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path)  # no pipelines/ dir at all
    tool = make_pipeline_inspect_tool(paths=paths)
    assert tool.executor({"op": "list"}) == {"pipelines": []}


def test_read_returns_parsed_shape(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    _write_pipeline(paths, "daily", _VALID)
    out = make_pipeline_inspect_tool(paths=paths).executor({"op": "read", "name": "daily"})
    assert out["name"] == "daily"
    assert out["description"] == "ingest stripe then build"
    assert out["seed_schedule"]["cron"] == "0 2 * * *"
    ids = [s["id"] for s in out["steps"]]
    assert ids == ["ingest", "transform"]
    transform = out["steps"][1]
    assert transform["depends_on"] == ["ingest"]
    assert transform["component"] == "stripe"
    assert transform["failure_mode"] == "fail"


def test_read_unknown_pipeline_errors(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    with pytest.raises(ToolExecutionError, match="ghost"):
        make_pipeline_inspect_tool(paths=paths).executor({"op": "read", "name": "ghost"})


def test_read_rejects_path_traversal(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    _write_pipeline(paths, "ok", _VALID)
    with pytest.raises(ToolExecutionError, match="outside the pipelines"):
        make_pipeline_inspect_tool(paths=paths).executor({"op": "read", "name": "../../etc/passwd"})


def test_read_surfaces_validation_error_on_bad_depends_on(tmp_path: Path) -> None:
    # load_pipeline IS the validate gate: a dangling depends_on must surface as a
    # structured ToolExecutionError so the engineer can self-correct.
    paths = _project(tmp_path)
    _write_pipeline(
        paths,
        "broken",
        """\
[[steps]]
id = "a"
type = "dlt"
component = "stripe"
depends_on = ["nope"]
""",
    )
    with pytest.raises(ToolExecutionError, match="nope"):
        make_pipeline_inspect_tool(paths=paths).executor({"op": "read", "name": "broken"})


def test_read_requires_a_name(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    with pytest.raises(ToolExecutionError, match="requires a 'name'"):
        make_pipeline_inspect_tool(paths=paths).executor({"op": "read"})


def test_unknown_op_errors(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    with pytest.raises(ToolExecutionError, match="Unknown"):
        make_pipeline_inspect_tool(paths=paths).executor({"op": "frobnicate"})


def test_tool_name_equals_grant_name(tmp_path: Path) -> None:
    # The binder precondition: injected.name == grant_name.
    paths = _project(tmp_path)
    assert make_pipeline_inspect_tool(paths=paths).name == "pipeline_inspect"
    assert make_pipeline_inspect_tool(paths=paths, name="custom").name == "custom"
