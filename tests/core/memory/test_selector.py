"""select_for_task: always-include conventions/standards, decisions gating, sidecars."""

from __future__ import annotations

from pathlib import Path

from carve.core.config.paths import ProjectPaths
from carve.core.memory.loader import MemoryLoader
from carve.core.memory.selector import select_for_task


def _project(root: Path) -> MemoryLoader:
    (root / "carve").mkdir(parents=True)
    (root / "carve" / "conventions.md").write_text("conv\n")
    (root / "carve" / "standards.md").write_text("std\n")
    (root / "carve" / "decisions.md").write_text("dec\n")
    (root / "pipelines").mkdir()
    (root / "pipelines" / "stripe.md").write_text("stripe notes\n")
    (root / "el" / "orders").mkdir(parents=True)
    (root / "el" / "orders" / "NOTES.md").write_text("orders notes\n")
    return MemoryLoader(ProjectPaths.from_root(root))


def test_conventions_and_standards_always_included(tmp_path: Path) -> None:
    loader = _project(tmp_path)
    b = select_for_task(
        classification="anything",
        pipeline_targets=[],
        el_targets=[],
        is_investigative=False,
        loader=loader,
    )
    assert b.conventions.contents == "conv\n"
    assert b.standards.contents == "std\n"
    assert b.decisions is None  # not investigative → excluded


def test_decisions_included_only_when_investigative(tmp_path: Path) -> None:
    loader = _project(tmp_path)
    plan_bundle = select_for_task(
        classification="modify_pipeline",
        pipeline_targets=[],
        el_targets=[],
        is_investigative=False,
        loader=loader,
    )
    assert plan_bundle.decisions is None

    ask_bundle = select_for_task(
        classification="ask",
        pipeline_targets=[],
        el_targets=[],
        is_investigative=True,
        loader=loader,
    )
    assert ask_bundle.decisions is not None
    assert ask_bundle.decisions.contents == "dec\n"


def test_sidecars_included_when_present_skipped_when_absent(tmp_path: Path) -> None:
    loader = _project(tmp_path)
    b = select_for_task(
        classification="",
        pipeline_targets=["stripe", "ghost"],
        el_targets=["orders", "ghost"],
        is_investigative=False,
        loader=loader,
    )
    assert set(b.pipeline_sidecars) == {"stripe"}  # ghost absent → skipped
    assert b.pipeline_sidecars["stripe"].contents == "stripe notes\n"
    assert set(b.el_sidecars) == {"orders"}
    assert b.el_sidecars["orders"].contents == "orders notes\n"
