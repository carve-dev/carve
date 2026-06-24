"""DbtStepExecutor: calls DbtBackend.run and maps DbtRunResult -> StepResult.

The executor is **backend-agnostic**: a stubbed ``DbtBackend`` stands in for
both ``local`` and a (deferred) ``managed`` backend, and the mapping is asserted
**backend-uniform** — an identical ``DbtRunResult`` maps to an identical
``StepResult`` regardless of which backend produced it. The backend factory is
injected so no real dbt is needed. An ``UnsupportedBackendError`` at construction
surfaces as a clean ``failed`` StepResult. A real-dbt run is optional/deferred
(mirrors ``tests/core/dbt_execution/test_local_backend.py`` with the injected
engine for offline).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import DbtStepConfig
from carve.core.dbt_execution.backend import DbtCommand
from carve.core.dbt_execution.local import UnsupportedBackendError
from carve.core.dbt_execution.result import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_SUCCESS,
    DbtRunResult,
    PerModelResult,
)
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_types.dbt import DbtStepExecutor


@pytest.fixture
def paths(tmp_path: Path) -> ProjectPaths:
    # A single detected dbt project so an omitted `component` resolves.
    (tmp_path / "dbt_project.yml").write_text("name: analytics\n", encoding="utf-8")
    (tmp_path / "pipelines").mkdir()
    return ProjectPaths.from_root(tmp_path)


class _FakeBackend:
    """A stub DbtBackend: records the command, returns a canned DbtRunResult."""

    def __init__(self, result: DbtRunResult) -> None:
        self._result = result
        self.commands: list[DbtCommand] = []

    def run(self, command: DbtCommand) -> DbtRunResult:
        self.commands.append(command)
        return self._result


def _factory_for(backend: _FakeBackend) -> Any:
    """An injected backend factory that returns the given fake backend."""

    def _factory(**_kwargs: Any) -> _FakeBackend:
        return backend

    return _factory


def _green_result() -> DbtRunResult:
    return DbtRunResult(
        status=STATUS_SUCCESS,
        per_model=[
            PerModelResult(unique_id="model.a.stg", name="stg", status="success"),
            PerModelResult(unique_id="model.a.dim", name="dim", status="success"),
        ],
        logs="ok\n",
        duration_ms=42,
    )


def _executor(backend: _FakeBackend) -> DbtStepExecutor:
    return DbtStepExecutor(dbt_executable="dbt", backend_factory=_factory_for(backend))


# --- command construction --------------------------------------------------


async def test_builds_dbt_command_from_step_and_run(paths: ProjectPaths) -> None:
    backend = _FakeBackend(_green_result())
    step = DbtStepConfig(
        id="stage",
        component=None,
        command="build",
        select="stg_stripe+",
        exclude="tag:wip",
        vars={"k": "v"},
        full_refresh=True,
    )

    await _executor(backend).execute(
        step=step, run=PipelineRun(pipeline="p", target="prod"), paths=paths
    )

    cmd = backend.commands[0]
    assert cmd.command == "build"
    assert cmd.select == ("stg_stripe+",)
    assert cmd.exclude == ("tag:wip",)
    assert cmd.target == "prod"
    assert cmd.full_refresh is True
    assert cmd.vars == {"k": "v"}


async def test_empty_selectors_become_empty_tuples(paths: ProjectPaths) -> None:
    backend = _FakeBackend(_green_result())
    await _executor(backend).execute(
        step=DbtStepConfig(id="s", command="run"),
        run=PipelineRun(pipeline="p"),
        paths=paths,
    )
    cmd = backend.commands[0]
    assert cmd.select == ()
    assert cmd.exclude == ()
    assert cmd.vars is None


# --- DbtRunResult -> StepResult mapping ------------------------------------


async def test_success_maps_to_succeeded_with_per_model_outputs(paths: ProjectPaths) -> None:
    backend = _FakeBackend(_green_result())
    result = await _executor(backend).execute(
        step=DbtStepConfig(id="s"), run=PipelineRun(pipeline="p"), paths=paths
    )

    assert result.status == "succeeded"
    assert result.outputs["status"] == STATUS_SUCCESS
    names = [pm["name"] for pm in result.outputs["per_model"]]
    assert names == ["stg", "dim"]
    assert result.duration_ms == 42
    assert result.log_lines == ["ok"]
    assert result.error_message is None


async def test_failed_node_maps_to_failed_with_message(paths: ProjectPaths) -> None:
    backend = _FakeBackend(
        DbtRunResult(
            status=STATUS_FAILED,
            per_model=[
                PerModelResult(unique_id="model.a.dim", name="dim", status="error", message="boom"),
            ],
        )
    )
    result = await _executor(backend).execute(
        step=DbtStepConfig(id="s", command="run"), run=PipelineRun(pipeline="p"), paths=paths
    )

    assert result.status == "failed"
    assert "dim (error)" in (result.error_message or "")
    assert result.outputs["status"] == STATUS_FAILED


async def test_failing_tests_surface_in_error_message(paths: ProjectPaths) -> None:
    backend = _FakeBackend(
        DbtRunResult(
            status=STATUS_FAILED,
            per_model=[
                PerModelResult(
                    unique_id="test.a.not_null", name="not_null", status="fail", failures=3
                ),
                PerModelResult(unique_id="test.a.unique", name="unique", status="pass"),
            ],
        )
    )
    result = await _executor(backend).execute(
        step=DbtStepConfig(id="s", command="test"), run=PipelineRun(pipeline="p"), paths=paths
    )

    assert result.status == "failed"
    assert "failing test(s)" in (result.error_message or "")
    assert "not_null (3)" in (result.error_message or "")


async def test_error_status_maps_to_failed_fail_closed(paths: ProjectPaths) -> None:
    backend = _FakeBackend(DbtRunResult(status=STATUS_ERROR, per_model=[]))
    result = await _executor(backend).execute(
        step=DbtStepConfig(id="s"), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "no readable run_results.json" in (result.error_message or "")


# --- FIX-DB1: invalid DbtCommand construction is a clean failed ------------


async def test_test_command_with_full_refresh_is_clean_failed(paths: ProjectPaths) -> None:
    # DbtStepConfig doesn't cross-validate command vs full_refresh, so
    # command="test" + full_refresh=True makes DbtCommand raise a pydantic
    # ValidationError. The executor must catch it and return a clean `failed`
    # StepResult — never let the exception escape execute() — and never run.
    backend = _FakeBackend(_green_result())
    step = DbtStepConfig(id="snap", command="test", full_refresh=True)

    result = await _executor(backend).execute(step=step, run=PipelineRun(pipeline="p"), paths=paths)

    assert result.status == "failed"
    assert "invalid dbt command for step snap" in (result.error_message or "")
    assert backend.commands == []  # never ran


async def test_snapshot_command_with_full_refresh_is_clean_failed(paths: ProjectPaths) -> None:
    backend = _FakeBackend(_green_result())
    step = DbtStepConfig(id="s", command="snapshot", full_refresh=True)

    result = await _executor(backend).execute(step=step, run=PipelineRun(pipeline="p"), paths=paths)

    assert result.status == "failed"
    assert "invalid dbt command for step s" in (result.error_message or "")
    assert backend.commands == []


# --- backend uniformity (the spec's hard requirement) ----------------------


async def test_mapping_is_backend_uniform(paths: ProjectPaths) -> None:
    # The SAME DbtRunResult, produced by two different "backends" (here: two
    # fake backends standing in for local + managed), maps to an IDENTICAL
    # StepResult — the executor never branches on which backend ran.
    canned = _green_result()
    local_backend = _FakeBackend(canned)
    managed_backend = _FakeBackend(canned)

    step = DbtStepConfig(id="s")
    run = PipelineRun(pipeline="p")
    from_local = await _executor(local_backend).execute(step=step, run=run, paths=paths)
    from_managed = await _executor(managed_backend).execute(step=step, run=run, paths=paths)

    assert from_local.model_dump() == from_managed.model_dump()


# --- construction / resolution failures ------------------------------------


async def test_unsupported_backend_surfaces_as_failed(paths: ProjectPaths) -> None:
    def _raising_factory(**_kwargs: Any) -> Any:
        raise UnsupportedBackendError("dbt backend 'snowflake-native' is not yet implemented")

    executor = DbtStepExecutor(dbt_executable="dbt", backend_factory=_raising_factory)
    result = await executor.execute(
        step=DbtStepConfig(id="s"), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "not yet implemented" in (result.error_message or "")


async def test_unresolvable_named_component_is_failed(tmp_path: Path) -> None:
    # No dbt project + a named component with no block -> resolution fails.
    (tmp_path / "pipelines").mkdir()
    paths = ProjectPaths.from_root(tmp_path)
    backend = _FakeBackend(_green_result())
    result = await _executor(backend).execute(
        step=DbtStepConfig(id="s", component="missing"),
        run=PipelineRun(pipeline="p"),
        paths=paths,
    )
    assert result.status == "failed"
    assert "did not resolve" in (result.error_message or "")
    assert backend.commands == []  # never ran
