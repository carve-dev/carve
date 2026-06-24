"""build_step_executor_registry + execute_pipeline over a dlt -> dbt -> sql run.

The registry-builder wires all three concrete executors with their injectable
seams so the whole thing runs creds-free: the dlt run mechanism is a fake that
writes a real load package, the dbt backend is a fake returning a canned green
result, and the sql connection is a real shared in-process DuckDB. The pipeline
loads from a synthetic ``pipelines/stripe.toml``-shaped TOML (exercising
``load_pipeline`` + component resolution) and runs end to end via
``execute_pipeline`` + the real registry — asserting topological order, threaded
``outputs``, and the terminal ``RunResult.status``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import ConnectionsConfig, DuckDBConnection
from carve.core.dbt_execution.backend import DbtCommand
from carve.core.dbt_execution.result import STATUS_SUCCESS, DbtRunResult, PerModelResult
from carve.runtime.execute_pipeline import execute_pipeline
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_types.connections import ResolvedConnection
from carve.runtime.step_types.dlt import DltRunOutcome
from carve.runtime.step_types.registry import build_step_executor_registry

_PIPELINE_TOML = """
[pipeline]
description = "ingest + stage + refresh"

[[steps]]
id = "ingest_stripe"
type = "dlt"
component = "stripe_charges"
depends_on = []

[[steps]]
id = "stage_stripe"
type = "dbt"
command = "build"
select = "stg_stripe_charges+"
depends_on = ["ingest_stripe"]

[[steps]]
id = "refresh_search"
type = "sql"
file = "sql/refresh.sql"
connection = "local"
depends_on = ["stage_stripe"]
[steps.jinja_vars]
loaded_rows = "{{ steps.ingest_stripe.outputs.tables | length }}"
"""


def _project(tmp_path: Path) -> ProjectPaths:
    """A simple-mode project: an el/ dlt component, a root dbt project, a sql file."""
    (tmp_path / "el" / "stripe_charges" / "scripts").mkdir(parents=True)
    (tmp_path / "el" / "stripe_charges" / "scripts" / "__init__.py").write_text(
        "def run():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "dbt_project.yml").write_text("name: analytics\n", encoding="utf-8")
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "stripe.toml").write_text(_PIPELINE_TOML, encoding="utf-8")
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "refresh.sql").write_text(
        "SELECT {{ vars.loaded_rows }} AS loaded_rows", encoding="utf-8"
    )
    return ProjectPaths.from_root(tmp_path)


def _dlt_run_fn() -> Any:
    """A fake dlt run mechanism that writes a real load package.

    The package's ``load_id`` is ``str(unix_time)`` for **now** (when the run
    executes), so it is fresh relative to ``run.started_at`` — the executor's
    recency filter (FIX-D2 residual) only trusts a package from this run, and a
    pinned-in-the-past load_id would (correctly) read as a stale prior-run
    package and fail. A real dlt run writes a current load_id the same way.
    """
    import time as _time

    def _run(**kwargs: Any) -> DltRunOutcome:
        data_dir = Path(kwargs["env"]["DLT_DATA_DIR"])
        load_id = str(_time.time())
        pkg = data_dir / "pipelines" / "stripe_charges" / "load" / "loaded" / load_id
        (pkg / "completed_jobs").mkdir(parents=True)
        for t in ("charges", "_dlt_pipeline_state"):
            (pkg / "completed_jobs" / f"{t}.hash.0.insert_values.gz").write_text("")
        (pkg / "applied_schema_updates.json").write_text(json.dumps({"charges": {"columns": {}}}))
        (pkg / "load_package_state.json").write_text(
            json.dumps(
                {"load_metrics": {"charges.h.gz": {"table_name": "charges", "state": "completed"}}}
            )
        )
        return DltRunOutcome(returncode=0, output="loaded", duration_ms=3)

    return _run


def _dbt_backend_factory() -> Any:
    """A fake dbt backend factory returning a canned green result."""

    class _Backend:
        def run(self, command: DbtCommand) -> DbtRunResult:
            return DbtRunResult(
                status=STATUS_SUCCESS,
                per_model=[
                    PerModelResult(
                        unique_id="model.a.stg", name="stg_stripe_charges", status="success"
                    ),
                ],
                duration_ms=7,
            )

    def _factory(**_kwargs: Any) -> _Backend:
        return _Backend()

    return _factory


def _duckdb_factory() -> Any:
    """A shared in-process DuckDB connection factory (creds-free)."""
    from carve.core.connectors.duckdb import DIALECT, DuckDBConnection

    resolved = ResolvedConnection(DuckDBConnection(database=":memory:"), DIALECT)

    def _factory(_name: str, _config: ConnectionsConfig) -> ResolvedConnection:
        return resolved

    return _factory


@pytest.fixture
def connections() -> ConnectionsConfig:
    return ConnectionsConfig(duckdb={"local": DuckDBConnection()})


async def test_dlt_dbt_sql_pipeline_runs_end_to_end(
    tmp_path: Path, connections: ConnectionsConfig
) -> None:
    paths = _project(tmp_path)
    registry = build_step_executor_registry(
        connections=connections,
        dbt_executable="dbt",
        dlt_run_fn=_dlt_run_fn(),
        dbt_backend_factory=_dbt_backend_factory(),
        connection_factory=_duckdb_factory(),
    )

    result = await execute_pipeline(
        PipelineRun(pipeline="stripe", target="dev"),
        paths=paths,
        registry=registry,
    )

    # All three steps succeeded, in topological order.
    assert result.status == "succeeded"
    assert result.completed == frozenset({"ingest_stripe", "stage_stripe", "refresh_search"})
    assert result.failed == frozenset()

    # dlt outputs threaded forward.
    assert result.outputs["ingest_stripe"]["tables"] == ["charges"]
    assert result.outputs["stage_stripe"]["status"] == STATUS_SUCCESS

    # The sql step's file body rendered the threaded cross-step value (the dlt
    # step loaded one user table -> loaded_rows == 1).
    assert result.outputs["refresh_search"]["rows"] == [{"loaded_rows": 1}]


async def test_registry_registers_all_three_types(connections: ConnectionsConfig) -> None:
    registry = build_step_executor_registry(connections=connections, dbt_executable="dbt")
    assert "dlt" in registry
    assert "dbt" in registry
    assert "sql" in registry
    assert registry.lookup("dlt").step_type == "dlt"
    assert registry.lookup("dbt").step_type == "dbt"
    assert registry.lookup("sql").step_type == "sql"


async def test_dbt_failure_fails_the_run_and_skips_nothing_downstream_succeeds(
    tmp_path: Path, connections: ConnectionsConfig
) -> None:
    # A failing dbt step under the default `fail` mode halts the run: the
    # downstream sql step never runs, the run status is `failed`.
    paths = _project(tmp_path)

    def _failing_factory() -> Any:
        class _Backend:
            def run(self, command: DbtCommand) -> DbtRunResult:
                return DbtRunResult(
                    status="failed",
                    per_model=[
                        PerModelResult(
                            unique_id="model.a.stg",
                            name="stg_stripe_charges",
                            status="error",
                            message="boom",
                        ),
                    ],
                )

        def _factory(**_kwargs: Any) -> _Backend:
            return _Backend()

        return _factory

    registry = build_step_executor_registry(
        connections=connections,
        dbt_executable="dbt",
        dlt_run_fn=_dlt_run_fn(),
        dbt_backend_factory=_failing_factory(),
        connection_factory=_duckdb_factory(),
    )

    result = await execute_pipeline(
        PipelineRun(pipeline="stripe", target="dev"),
        paths=paths,
        registry=registry,
    )

    assert result.status == "failed"
    assert "ingest_stripe" in result.completed
    assert "stage_stripe" in result.failed
    # The default `fail` mode halts: the downstream sql step never started.
    assert "refresh_search" not in result.completed
