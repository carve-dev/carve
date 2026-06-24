"""Regression tests for the adversarial-review fixes to ``execute_pipeline``.

Two fixes the review surfaced (both at the executor/DAG-walk seam):

- **A raising ``StepExecutor`` is degraded to a ``failed`` StepResult** that
  flows through the failure-mode state machine, instead of propagating out of
  ``asyncio.gather`` and crashing the whole run (the real Unit-2 dlt/dbt/sql
  executors raise: subprocess/network/parse errors).
- **A warn/continue-failed upstream step is visible in the downstream Jinja
  ``steps`` namespace** with its real status — a template referencing
  ``steps.<id>.status`` resolves instead of hitting ``StrictUndefined``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import (
    DbtStepConfig,
    DltStepConfig,
    FailureMode,
    Pipeline,
)
from carve.runtime.execute_pipeline import execute_pipeline
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_executor import StepExecutorRegistry, StepResult

if TYPE_CHECKING:
    from carve.core.config.pipeline_schema import PipelineStep


@pytest.fixture
def paths(tmp_path: Path) -> ProjectPaths:
    (tmp_path / "pipelines").mkdir()
    return ProjectPaths.from_root(tmp_path)


async def _no_sleep(_seconds: float) -> None:
    return None


class _ConfigurableStub:
    """A stub ``StepExecutor``: raises ``RuntimeError`` for ids in ``raises``,
    otherwise returns the mapped result (default ``succeeded``). Records the
    rendered Jinja vars each step was launched with."""

    step_type = "dlt"

    def __init__(
        self,
        *,
        raises: frozenset[str] = frozenset(),
        results: dict[str, StepResult] | None = None,
    ) -> None:
        self._raises = set(raises)
        self._results = results or {}
        self.seen_jinja: dict[str, dict[str, str]] = {}
        self.ran: list[str] = []

    async def execute(
        self, *, step: PipelineStep, run: PipelineRun, paths: ProjectPaths
    ) -> StepResult:
        self.ran.append(step.id)
        self.seen_jinja[step.id] = dict(step.jinja_vars)
        await asyncio.sleep(0)
        if step.id in self._raises:
            raise RuntimeError(f"boom in {step.id}")
        return self._results.get(step.id, StepResult(status="succeeded"))


def _registry(ex: _ConfigurableStub) -> StepExecutorRegistry:
    registry = StepExecutorRegistry()
    registry.register(ex)
    return registry


def _dlt(
    step_id: str,
    *,
    deps: list[str] | None = None,
    mode: str = "fail",
    jinja_vars: dict[str, str] | None = None,
) -> DltStepConfig:
    return DltStepConfig(
        id=step_id,
        component="c",
        depends_on=deps or [],
        failure_mode=FailureMode(mode=mode),  # type: ignore[arg-type]
        jinja_vars=jinja_vars or {},
    )


# --------------------------------------------------------------------------
# FIX 2 — a raising executor is degraded, not fatal
# --------------------------------------------------------------------------


async def test_raising_executor_under_warn_is_tolerated_not_fatal(paths: ProjectPaths) -> None:
    # `a` (warn) raises; `b` is an independent sibling that must still run.
    pipeline = Pipeline(name="p", steps=[_dlt("a", mode="warn"), _dlt("b")])
    stub = _ConfigurableStub(raises=frozenset({"a"}))
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    # The raise was caught -> failed StepResult -> tolerated under warn -> partial.
    assert result.status == "partial"
    assert "a" in result.failed
    # The concurrent sibling still ran and succeeded (its result was not lost).
    assert "b" in result.completed
    assert "b" in stub.ran


async def test_unregistered_step_type_degrades_not_crashes(paths: ProjectPaths) -> None:
    # `b` is a dbt step but only a dlt executor is registered, so
    # `registry.lookup("dbt")` raises during launch. It must degrade to a
    # failed step (tolerated under warn), not escape the wave and discard the
    # dlt sibling `a`.
    pipeline = Pipeline(
        name="p",
        steps=[
            _dlt("a"),
            DbtStepConfig(
                id="b",
                depends_on=[],
                failure_mode=FailureMode(mode="warn"),
            ),
        ],
    )
    stub = _ConfigurableStub()  # registers "dlt" only
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "partial"
    assert "b" in result.failed
    assert "a" in result.completed


async def test_raising_executor_under_fail_fails_run_without_crashing(
    paths: ProjectPaths,
) -> None:
    pipeline = Pipeline(name="p", steps=[_dlt("a", mode="fail")])
    stub = _ConfigurableStub(raises=frozenset({"a"}))
    # execute_pipeline must NOT propagate the RuntimeError; it resolves to failed.
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "failed"
    assert "a" in result.failed


# --------------------------------------------------------------------------
# FIX 4 — a warn-failed upstream is visible in the downstream Jinja namespace
# --------------------------------------------------------------------------


async def test_warn_failed_upstream_visible_in_downstream_jinja(paths: ProjectPaths) -> None:
    # `a` fails under warn; `b` depends on `a` and references steps.a.status.
    # Before the fix, steps.a was absent -> StrictUndefined -> `b` failed to render.
    pipeline = Pipeline(
        name="p",
        steps=[
            _dlt("a", mode="warn"),
            _dlt("b", deps=["a"], jinja_vars={"upstream_status": "{{ steps.a.status }}"}),
        ],
    )
    stub = _ConfigurableStub(
        results={"a": StepResult(status="failed", error_message="boom")},
    )
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    # `b` rendered the failed upstream's real status rather than crashing.
    assert stub.seen_jinja["b"]["upstream_status"] == "failed"
    assert "b" in result.completed
