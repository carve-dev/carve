"""Tests for the five failure modes + run-status derivation.

Covers the *pipelines* spec's Unit (failure modes each) bar: one test per
mode (``fail``/``warn``/``continue``/``retry``/``skip_downstream``)
exercising the set-mutation transitions in the spec's table, plus the
``succeeded``/``failed``/``partial`` status derivation.

These tests drive ``apply_failure_mode`` directly (the classification
half); the ``retry`` *orchestration* (attempt loop + backoff) lives in
``execute_pipeline`` and is proven end-to-end in ``test_execute_pipeline``.
A ``retry``-mode step reaching ``apply_failure_mode`` with a failing result
has already exhausted its attempts, so it classifies as ``fail`` here.
"""

from __future__ import annotations

from carve.core.config.pipeline_schema import DltStepConfig, FailureMode, Pipeline
from carve.runtime.failure_modes import RunState, apply_failure_mode, derive_run_status
from carve.runtime.pipeline_dag import PipelineDAG
from carve.runtime.step_executor import StepResult


def _dag(*edges: tuple[str, list[str]]) -> PipelineDAG:
    steps = [DltStepConfig(id=sid, component="c", depends_on=deps) for sid, deps in edges]
    return PipelineDAG(Pipeline(name="p", steps=steps))


def _step(step_id: str, mode: str, deps: list[str] | None = None) -> DltStepConfig:
    return DltStepConfig(
        id=step_id,
        component="c",
        depends_on=deps or [],
        failure_mode=FailureMode(mode=mode),  # type: ignore[arg-type]
    )


_OK = StepResult(status="succeeded", outputs={"rows": 1})
_FAIL = StepResult(status="failed", error_message="boom")


# ---------------------------------------------------------------------------
# On success: every mode adds to completed
# ---------------------------------------------------------------------------


def test_success_marks_completed_for_any_mode() -> None:
    for mode in ("fail", "warn", "continue", "retry", "skip_downstream"):
        state = RunState()
        dag = _dag(("a", []))
        apply_failure_mode(_step("a", mode), _OK, dag, state)
        assert state.completed == {"a"}
        assert not state.failed
        assert not state.halted
        assert derive_run_status(state) == "succeeded"


# ---------------------------------------------------------------------------
# fail
# ---------------------------------------------------------------------------


def test_fail_mode_halts_run() -> None:
    state = RunState()
    dag = _dag(("a", []), ("b", ["a"]))
    apply_failure_mode(_step("a", "fail"), _FAIL, dag, state)
    assert state.failed == {"a"}
    assert state.halted is True
    assert derive_run_status(state) == "failed"


# ---------------------------------------------------------------------------
# warn
# ---------------------------------------------------------------------------


def test_warn_mode_records_warning_and_continues() -> None:
    state = RunState()
    dag = _dag(("a", []), ("b", ["a"]))
    apply_failure_mode(_step("a", "warn"), _FAIL, dag, state)
    assert state.failed == {"a"}
    assert state.halted is False
    assert state.tolerated_failures == ["a"]
    assert len(state.warnings) == 1
    assert "boom" in state.warnings[0]
    # The dep resolved (by fall-through), so downstream is now ready.
    assert {s.id for s in dag.ready_steps(state.completed, state.failed, state.skipped)} == {"b"}
    assert derive_run_status(state) == "partial"


# ---------------------------------------------------------------------------
# continue
# ---------------------------------------------------------------------------


def test_continue_mode_continues_without_warning() -> None:
    state = RunState()
    dag = _dag(("a", []), ("b", ["a"]))
    apply_failure_mode(_step("a", "continue"), _FAIL, dag, state)
    assert state.failed == {"a"}
    assert state.halted is False
    assert state.tolerated_failures == ["a"]
    assert state.warnings == []  # continue records no warning text
    assert {s.id for s in dag.ready_steps(state.completed, state.failed, state.skipped)} == {"b"}
    assert derive_run_status(state) == "partial"


# ---------------------------------------------------------------------------
# retry (exhausted -> fail; the loop itself is in execute_pipeline)
# ---------------------------------------------------------------------------


def test_retry_mode_exhausted_classifies_as_fail() -> None:
    state = RunState()
    dag = _dag(("a", []))
    # A failing result reaching here means retries were already exhausted.
    apply_failure_mode(_step("a", "retry"), _FAIL, dag, state)
    assert state.failed == {"a"}
    assert state.halted is True
    assert derive_run_status(state) == "failed"


# ---------------------------------------------------------------------------
# skip_downstream
# ---------------------------------------------------------------------------


def test_skip_downstream_marks_dependents_and_keeps_siblings() -> None:
    # a fails (skip_downstream); b depends on a (-> skipped); c is a sibling.
    state = RunState()
    dag = _dag(("a", []), ("b", ["a"]), ("c", []))
    apply_failure_mode(_step("a", "skip_downstream", []), _FAIL, dag, state)
    assert state.failed == {"a"}
    assert state.skipped == {"b"}
    assert state.halted is False
    # The sibling c is still ready (it does not depend on a).
    ready_ids = {s.id for s in dag.ready_steps(state.completed, state.failed, state.skipped)}
    assert "c" in ready_ids
    assert "b" not in ready_ids  # b was skipped
    assert derive_run_status(state) == "partial"


def test_skip_downstream_marks_transitive_dependents() -> None:
    state = RunState()
    dag = _dag(("a", []), ("b", ["a"]), ("c", ["b"]))
    apply_failure_mode(_step("a", "skip_downstream"), _FAIL, dag, state)
    assert state.skipped == {"b", "c"}


# ---------------------------------------------------------------------------
# Status derivation combinations
# ---------------------------------------------------------------------------


def test_all_success_is_succeeded() -> None:
    state = RunState()
    dag = _dag(("a", []), ("b", ["a"]))
    apply_failure_mode(_step("a", "fail"), _OK, dag, state)
    apply_failure_mode(_step("b", "fail"), _OK, dag, state)
    assert derive_run_status(state) == "succeeded"


def test_skips_alone_make_partial() -> None:
    state = RunState()
    state.completed.add("a")
    state.skipped.add("b")
    assert derive_run_status(state) == "partial"
