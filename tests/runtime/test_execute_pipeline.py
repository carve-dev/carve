"""End-to-end mechanics of ``execute_pipeline`` over STUB executors.

This is the unit's additive coverage (the spec proves these via Integration
tests that need the real executors + runtime; here we prove the *mechanics*
with stubs, ahead of those): topological order, intra-level parallelism, all
five failure modes end-to-end, ``skip_downstream`` (dependents skipped while
siblings run), ``retry`` (fail-twice-then-succeed under ``max_attempts=3``),
and Jinja cross-step output threading — with NO real dlt/dbt/sql executor and
NO scheduler/worker.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import (
    DltStepConfig,
    FailureMode,
    Pipeline,
    SqlStepConfig,
)
from carve.runtime.execute_pipeline import RunResult, execute_pipeline
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_executor import StepExecutorRegistry, StepResult

if TYPE_CHECKING:
    from carve.core.config.pipeline_schema import PipelineStep


@pytest.fixture
def paths(tmp_path: Path) -> ProjectPaths:
    (tmp_path / "pipelines").mkdir()
    return ProjectPaths.from_root(tmp_path)


async def _no_sleep(_seconds: float) -> None:
    """A retry-backoff sleeper that doesn't actually sleep."""
    return None


class StubExecutor:
    """A fake :class:`StepExecutor` driven by a per-step-id result map.

    Records the order steps started in, and brackets each execution with a
    short ``await asyncio.sleep(0)`` + a shared "in-flight" counter so a test
    can assert that independent steps overlapped (intra-level parallelism).
    A step id mapped to a *list* of results yields them across successive
    attempts (for retry tests); a single result repeats.
    """

    def __init__(
        self,
        step_type: str,
        results: dict[str, StepResult | list[StepResult]],
    ) -> None:
        self._step_type = step_type
        self._results = results
        self.started_order: list[str] = []
        self._attempts: dict[str, int] = defaultdict(int)
        self.max_concurrent = 0
        self._in_flight = 0
        self.seen_jinja: dict[str, dict[str, str]] = {}

    @property
    def step_type(self) -> str:
        return self._step_type

    async def execute(
        self,
        *,
        step: PipelineStep,
        run: PipelineRun,
        paths: ProjectPaths,
    ) -> StepResult:
        self.started_order.append(step.id)
        self.seen_jinja[step.id] = dict(step.jinja_vars)
        self._in_flight += 1
        self.max_concurrent = max(self.max_concurrent, self._in_flight)
        try:
            # Yield control so genuinely-parallel launches overlap here.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            spec = self._results.get(step.id, StepResult(status="succeeded"))
            if isinstance(spec, list):
                idx = min(self._attempts[step.id], len(spec) - 1)
                self._attempts[step.id] += 1
                return spec[idx]
            return spec
        finally:
            self._in_flight -= 1


def _registry(*executors: StubExecutor) -> StepExecutorRegistry:
    registry = StepExecutorRegistry()
    for ex in executors:
        registry.register(ex)
    return registry


def _dlt(step_id: str, deps: list[str] | None = None, mode: str = "fail") -> DltStepConfig:
    return DltStepConfig(
        id=step_id,
        component="c",
        depends_on=deps or [],
        failure_mode=FailureMode(mode=mode),  # type: ignore[arg-type]
    )


def _run(pipeline: Pipeline, **kwargs: object) -> Callable[[], RunResult]:
    """Return a zero-arg callable that runs the pipeline to completion."""

    def _go() -> RunResult:
        return asyncio.run(
            execute_pipeline(
                PipelineRun(pipeline=pipeline.name),
                paths=ProjectPaths.from_root(Path.cwd()),
                registry=kwargs["registry"],  # type: ignore[arg-type]
                pipeline=pipeline,
                sleeper=_no_sleep,
                **{k: v for k, v in kwargs.items() if k != "registry"},  # type: ignore[arg-type]
            )
        )

    return _go


# ---------------------------------------------------------------------------
# Topological order
# ---------------------------------------------------------------------------


async def test_executes_in_topological_order(paths: ProjectPaths) -> None:
    pipeline = Pipeline(
        name="p",
        steps=[_dlt("a"), _dlt("b", ["a"]), _dlt("c", ["b"])],
    )
    stub = StubExecutor("dlt", {})
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert stub.started_order == ["a", "b", "c"]
    assert result.status == "succeeded"
    assert result.completed == frozenset({"a", "b", "c"})


# ---------------------------------------------------------------------------
# Parallelism
# ---------------------------------------------------------------------------


async def test_independent_steps_run_concurrently(paths: ProjectPaths) -> None:
    # a -> {b, c, d}; b, c, d are independent and should overlap.
    pipeline = Pipeline(
        name="p",
        steps=[_dlt("a"), _dlt("b", ["a"]), _dlt("c", ["a"]), _dlt("d", ["a"])],
    )
    stub = StubExecutor("dlt", {})
    await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert stub.max_concurrent >= 3  # b, c, d overlapped


async def test_available_slots_caps_concurrency(paths: ProjectPaths) -> None:
    pipeline = Pipeline(
        name="p",
        steps=[_dlt("a"), _dlt("b", ["a"]), _dlt("c", ["a"]), _dlt("d", ["a"])],
    )
    stub = StubExecutor("dlt", {})
    await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        available_slots=2,
        sleeper=_no_sleep,
    )
    assert stub.max_concurrent <= 2


# ---------------------------------------------------------------------------
# Failure modes end-to-end
# ---------------------------------------------------------------------------


async def test_fail_mode_halts_downstream(paths: ProjectPaths) -> None:
    pipeline = Pipeline(
        name="p",
        steps=[_dlt("a", mode="fail"), _dlt("b", ["a"]), _dlt("c", ["b"])],
    )
    stub = StubExecutor("dlt", {"a": StepResult(status="failed", error_message="x")})
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "failed"
    assert result.failed == frozenset({"a"})
    assert stub.started_order == ["a"]  # b, c never launched


async def test_warn_mode_completes_partial_and_continues(paths: ProjectPaths) -> None:
    pipeline = Pipeline(
        name="p",
        steps=[_dlt("a", mode="warn"), _dlt("b", ["a"])],
    )
    stub = StubExecutor("dlt", {"a": StepResult(status="failed", error_message="bad refresh")})
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "partial"
    assert "b" in stub.started_order  # downstream still ran
    assert any("bad refresh" in w for w in result.warnings)


async def test_continue_mode_completes_partial(paths: ProjectPaths) -> None:
    pipeline = Pipeline(
        name="p",
        steps=[_dlt("a", mode="continue"), _dlt("b", ["a"])],
    )
    stub = StubExecutor("dlt", {"a": StepResult(status="failed")})
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "partial"
    assert "b" in stub.started_order
    assert result.warnings == []


async def test_skip_downstream_skips_dependents_runs_siblings(paths: ProjectPaths) -> None:
    # a (skip_downstream) fails; b depends on a (-> skipped); c is a sibling.
    pipeline = Pipeline(
        name="p",
        steps=[_dlt("a", mode="skip_downstream"), _dlt("b", ["a"]), _dlt("c")],
    )
    stub = StubExecutor("dlt", {"a": StepResult(status="failed")})
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "partial"
    assert result.skipped == frozenset({"b"})
    assert "c" in stub.started_order  # sibling ran
    assert "b" not in stub.started_order  # dependent never launched


async def test_retry_fails_twice_then_succeeds(paths: ProjectPaths) -> None:
    pipeline = Pipeline(
        name="p",
        steps=[
            DltStepConfig(
                id="ingest",
                component="c",
                failure_mode=FailureMode(mode="retry", max_attempts=3),
            )
        ],
    )
    stub = StubExecutor(
        "dlt",
        {
            "ingest": [
                StepResult(status="failed", error_message="transient 1"),
                StepResult(status="failed", error_message="transient 2"),
                StepResult(status="succeeded", outputs={"rows": 10}),
            ]
        },
    )
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "succeeded"
    assert result.completed == frozenset({"ingest"})
    # Three attempts: fail, fail, succeed.
    assert stub.started_order == ["ingest", "ingest", "ingest"]
    assert result.outputs["ingest"] == {"rows": 10}


async def test_retry_exhausted_fails_run(paths: ProjectPaths) -> None:
    pipeline = Pipeline(
        name="p",
        steps=[
            DltStepConfig(
                id="ingest",
                component="c",
                failure_mode=FailureMode(mode="retry", max_attempts=2),
            )
        ],
    )
    stub = StubExecutor("dlt", {"ingest": StepResult(status="failed", error_message="always")})
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "failed"
    assert stub.started_order == ["ingest", "ingest"]  # exactly max_attempts


# ---------------------------------------------------------------------------
# Jinja cross-step output threading at launch
# ---------------------------------------------------------------------------


async def test_jinja_threads_upstream_output_into_downstream(paths: ProjectPaths) -> None:
    notify = SqlStepConfig(
        id="notify",
        file="sql/notify.sql",
        connection="prod",
        depends_on=["ingest"],
        jinja_vars={"loaded_rows": "{{ steps.ingest.outputs.rows_loaded }}"},
    )
    pipeline = Pipeline(name="p", steps=[_dlt("ingest"), notify])
    dlt_stub = StubExecutor(
        "dlt", {"ingest": StepResult(status="succeeded", outputs={"rows_loaded": 4200})}
    )
    sql_stub = StubExecutor("sql", {})
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(dlt_stub, sql_stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "succeeded"
    # The downstream sql step saw the rendered upstream output value.
    assert sql_stub.seen_jinja["notify"] == {"loaded_rows": "4200"}


async def test_jinja_render_failure_fails_the_step(paths: ProjectPaths) -> None:
    # The downstream references an output the upstream never emitted ->
    # StrictUndefined render error -> the step is a failure (default `fail`).
    notify = SqlStepConfig(
        id="notify",
        file="sql/notify.sql",
        connection="prod",
        depends_on=["ingest"],
        jinja_vars={"x": "{{ steps.ingest.outputs.never }}"},
    )
    pipeline = Pipeline(name="p", steps=[_dlt("ingest"), notify])
    dlt_stub = StubExecutor("dlt", {"ingest": StepResult(status="succeeded", outputs={})})
    sql_stub = StubExecutor("sql", {})
    result = await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(dlt_stub, sql_stub),
        pipeline=pipeline,
        sleeper=_no_sleep,
    )
    assert result.status == "failed"
    assert "notify" in result.failed
    assert "notify" not in sql_stub.started_order  # never reached the executor


# ---------------------------------------------------------------------------
# Sink injection (runtime-independence seam)
# ---------------------------------------------------------------------------


async def test_injected_sink_receives_lifecycle_calls(paths: ProjectPaths) -> None:
    events: list[tuple[str, str]] = []

    class RecordingSink:
        async def step_started(self, *, step: PipelineStep, run: PipelineRun, attempt: int) -> None:
            events.append(("started", step.id))

        async def step_finished(
            self, *, step: PipelineStep, run: PipelineRun, result: StepResult, attempt: int
        ) -> None:
            events.append(("finished", step.id))

    pipeline = Pipeline(name="p", steps=[_dlt("a"), _dlt("b", ["a"])])
    stub = StubExecutor("dlt", {})
    await execute_pipeline(
        PipelineRun(pipeline="p"),
        paths=paths,
        registry=_registry(stub),
        pipeline=pipeline,
        sink=RecordingSink(),
        sleeper=_no_sleep,
    )
    assert ("started", "a") in events
    assert ("finished", "a") in events
    assert ("started", "b") in events


# ---------------------------------------------------------------------------
# Loading from disk (the production path) works too
# ---------------------------------------------------------------------------


async def test_loads_pipeline_from_disk_when_not_injected(paths: ProjectPaths) -> None:
    el = paths.el_dir / "src"
    el.mkdir(parents=True)
    (el / "__init__.py").write_text("# dlt\n")
    (paths.pipelines_dir / "disk.toml").write_text(
        """
[[steps]]
id = "a"
type = "dlt"
component = "src"
"""
    )
    stub = StubExecutor("dlt", {})
    result = await execute_pipeline(
        PipelineRun(pipeline="disk"),
        paths=paths,
        registry=_registry(stub),
        sleeper=_no_sleep,
    )
    assert result.status == "succeeded"
    assert stub.started_order == ["a"]
