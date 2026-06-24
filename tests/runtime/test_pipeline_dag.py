"""Tests for ``PipelineDAG``: topological order, readiness, downstream.

Covers the *pipelines* spec's Unit (DAG) bar: correct topological order for
representative shapes (linear, fan-out, fan-in, diamond); ``ready_steps``
accounting for completed/failed/skipped; ``downstream_of`` returning the
transitive set; and cycle rejection at construction.
"""

from __future__ import annotations

import pytest

from carve.core.config.pipeline_schema import DltStepConfig, Pipeline, PipelineError
from carve.runtime.pipeline_dag import PipelineDAG


def _dag(*edges: tuple[str, list[str]]) -> PipelineDAG:
    """Build a DAG from ``(step_id, depends_on)`` pairs (all dlt steps)."""
    steps = [DltStepConfig(id=sid, component="c", depends_on=deps) for sid, deps in edges]
    return PipelineDAG(Pipeline(name="p", steps=steps))


def _precedes(order: list[str], earlier: str, later: str) -> bool:
    return order.index(earlier) < order.index(later)


# ---------------------------------------------------------------------------
# Topological order
# ---------------------------------------------------------------------------


def test_linear_topological_order() -> None:
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["b"]))
    assert dag.topological_order == ["a", "b", "c"]


def test_fan_out_topological_order() -> None:
    # a -> {b, c, d}
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["a"]))
    order = dag.topological_order
    assert order[0] == "a"
    for child in ("b", "c", "d"):
        assert _precedes(order, "a", child)


def test_fan_in_topological_order() -> None:
    # {a, b, c} -> d
    dag = _dag(("a", []), ("b", []), ("c", []), ("d", ["a", "b", "c"]))
    order = dag.topological_order
    assert order[-1] == "d"
    for parent in ("a", "b", "c"):
        assert _precedes(order, parent, "d")


def test_diamond_topological_order() -> None:
    # a -> {b, c} -> d
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"]))
    order = dag.topological_order
    assert _precedes(order, "a", "b")
    assert _precedes(order, "a", "c")
    assert _precedes(order, "b", "d")
    assert _precedes(order, "c", "d")


def test_topological_order_is_stable_by_declaration() -> None:
    # Independent roots come out in declaration order.
    dag = _dag(("z", []), ("y", []), ("x", []))
    assert dag.topological_order == ["z", "y", "x"]


# ---------------------------------------------------------------------------
# ready_steps
# ---------------------------------------------------------------------------


def test_ready_steps_initial_state_returns_roots() -> None:
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["a"]))
    ready = dag.ready_steps(set(), set(), set())
    assert [s.id for s in ready] == ["a"]


def test_ready_steps_unblocks_on_completed() -> None:
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["a"]))
    ready = dag.ready_steps({"a"}, set(), set())
    assert {s.id for s in ready} == {"b", "c"}


def test_ready_steps_treats_failed_as_resolved() -> None:
    # A `warn`/`continue` failure resolves the dep so downstream still runs.
    dag = _dag(("a", []), ("b", ["a"]))
    ready = dag.ready_steps(set(), {"a"}, set())
    assert {s.id for s in ready} == {"b"}


def test_ready_steps_treats_skipped_as_resolved() -> None:
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["b"]))
    # b skipped -> c becomes ready (its dep is resolved-by-skip).
    ready = dag.ready_steps({"a"}, set(), {"b"})
    assert {s.id for s in ready} == {"c"}


def test_ready_steps_excludes_already_resolved_steps() -> None:
    dag = _dag(("a", []), ("b", ["a"]))
    ready = dag.ready_steps({"a", "b"}, set(), set())
    assert ready == []


def test_ready_steps_waits_for_all_deps_in_fan_in() -> None:
    dag = _dag(("a", []), ("b", []), ("c", ["a", "b"]))
    # Only one parent done -> c not ready yet.
    ready = dag.ready_steps({"a"}, set(), set())
    assert {s.id for s in ready} == {"b"}


# ---------------------------------------------------------------------------
# downstream_of
# ---------------------------------------------------------------------------


def test_downstream_of_transitive_set() -> None:
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["b"]), ("d", ["c"]))
    assert dag.downstream_of("a") == {"b", "c", "d"}
    assert dag.downstream_of("b") == {"c", "d"}
    assert dag.downstream_of("d") == set()


def test_downstream_of_excludes_siblings() -> None:
    # a -> b; a -> c (b and c are siblings, neither downstream of the other).
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["a"]))
    assert dag.downstream_of("b") == set()
    assert dag.downstream_of("a") == {"b", "c"}


def test_downstream_of_diamond() -> None:
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"]))
    assert dag.downstream_of("a") == {"b", "c", "d"}
    assert dag.downstream_of("b") == {"d"}


# ---------------------------------------------------------------------------
# Cycle rejection at construction
# ---------------------------------------------------------------------------


def test_cycle_rejected_at_construction() -> None:
    with pytest.raises(PipelineError) as exc:
        _dag(("a", ["b"]), ("b", ["a"]))
    assert "cycle" in str(exc.value).lower()


def test_self_cycle_rejected() -> None:
    with pytest.raises(PipelineError):
        _dag(
            ("a", ["a"]),
        )


def test_dangling_dependency_rejected_at_construction() -> None:
    with pytest.raises(PipelineError) as exc:
        _dag(
            ("a", ["ghost"]),
        )
    assert "ghost" in str(exc.value)
