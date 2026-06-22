"""attach_memory_to_context: serializes the selected bundle, doesn't mutate input."""

from __future__ import annotations

from pathlib import Path

from carve.core.agents.orchestrator_hooks import attach_memory_to_context
from carve.core.config.paths import ProjectPaths
from carve.core.memory.loader import MemoryLoader


def _loader(root: Path) -> MemoryLoader:
    (root / "carve").mkdir(parents=True)
    (root / "carve" / "conventions.md").write_text("conv\n")
    (root / "carve" / "standards.md").write_text("std\n")
    (root / "carve" / "decisions.md").write_text("dec\n")
    (root / "pipelines").mkdir()
    (root / "pipelines" / "stripe.md").write_text("stripe notes\n")
    return MemoryLoader(ProjectPaths.from_root(root))


def test_attaches_memory_block(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    out = attach_memory_to_context(
        {"goal": "modify stripe"},
        classification="modify_pipeline",
        pipeline_targets=["stripe"],
        el_targets=[],
        is_investigative=False,
        loader=loader,
    )
    assert out["goal"] == "modify stripe"  # original keys preserved
    mem = out["memory"]
    assert mem["conventions"] == "conv\n"
    assert mem["standards"] == "std\n"
    assert mem["decisions"] is None  # not investigative
    assert mem["pipeline_notes"] == {"stripe": "stripe notes\n"}
    assert mem["el_notes"] == {}


def test_investigative_includes_decisions(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    out = attach_memory_to_context(
        {},
        classification="ask",
        pipeline_targets=[],
        el_targets=[],
        is_investigative=True,
        loader=loader,
    )
    assert out["memory"]["decisions"] == "dec\n"


def test_does_not_mutate_input_context(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    original: dict[str, object] = {"goal": "x"}
    out = attach_memory_to_context(
        original,
        classification="",
        pipeline_targets=[],
        el_targets=[],
        is_investigative=False,
        loader=loader,
    )
    assert "memory" not in original  # input untouched
    assert out is not original
