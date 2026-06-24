"""The registry-builder — wire the three concrete executors with their seams.

:func:`build_step_executor_registry` constructs a
:class:`~carve.runtime.step_executor.StepExecutorRegistry` and registers the
``dlt``/``dbt``/``sql`` executors, threading through every injectable seam so
the whole pipeline runs **creds-free** in tests:

* the **dlt run mechanism** (the venv/subprocess runner), so dlt tests never
  spawn a real venv except the one ``importorskip("dlt")``-gated real load;
* the **dbt engine path + backend factory**, so dbt runs against an injected
  fake engine with no real dbt installed;
* the **connection factory** (name → connector), DuckDB-default, so sql runs
  in-process against DuckDB.

``execute_pipeline(run, paths=…, registry=build_step_executor_registry(…))``
then drives a real ``dlt → dbt → sql`` pipeline. The *live* wiring of this
builder into the orchestrator/worker is Increment-4 runtime's; this unit ships
the builder + its injection points, exercised by tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from carve.runtime.step_executor import StepExecutorRegistry
from carve.runtime.step_types.dbt import (
    DEFAULT_DBT_TIMEOUT_SECONDS,
    BackendFactory,
    DbtStepExecutor,
)
from carve.runtime.step_types.dlt import (
    DEFAULT_DLT_TIMEOUT_SECONDS,
    DltRunFn,
    DltStepExecutor,
)
from carve.runtime.step_types.sql import (
    DEFAULT_ROW_CAP,
    DEFAULT_SQL_TIMEOUT_SECONDS,
    SqlStepExecutor,
)

if TYPE_CHECKING:
    from carve.core.config.schema import ComponentConfig, ConnectionsConfig
    from carve.runtime.step_types.connections import ConnectionFactory


def build_step_executor_registry(
    *,
    connections: ConnectionsConfig,
    dbt_executable: str,
    components: dict[str, ComponentConfig] | None = None,
    dlt_run_fn: DltRunFn | None = None,
    dbt_backend_factory: BackendFactory | None = None,
    connection_factory: ConnectionFactory | None = None,
    dlt_timeout_seconds: int = DEFAULT_DLT_TIMEOUT_SECONDS,
    dbt_timeout_seconds: int = DEFAULT_DBT_TIMEOUT_SECONDS,
    sql_timeout_seconds: int = DEFAULT_SQL_TIMEOUT_SECONDS,
    sql_row_cap: int = DEFAULT_ROW_CAP,
) -> StepExecutorRegistry:
    """Build a registry wired to the three concrete executors + their seams.

    Args:
        connections: The ``[connections.*]`` config the sql executor's factory
            resolves names against.
        dbt_executable: The resolved dbt engine binary (injected, never
            installed) the dbt executor's backend runs.
        components: ``[components.*]`` blocks for dlt/dbt name resolution
            (defaults to empty == simple mode).
        dlt_run_fn: The injected dlt run mechanism (defaults to the shipped
            venv/subprocess primitive).
        dbt_backend_factory: The injected dbt backend constructor (defaults to
            the shipped ``build_backend`` → ``LocalDbtBackend``).
        connection_factory: The injected name → connector resolver (defaults to
            DuckDB-first ``resolve_connection``).
        dlt_timeout_seconds / dbt_timeout_seconds / sql_timeout_seconds: Per-step
            wall-clock budgets (the spec's 4h / 1h / 5min defaults).
        sql_row_cap: First-N rows the sql executor captures into ``outputs``.

    Returns:
        A :class:`StepExecutorRegistry` with ``dlt``/``dbt``/``sql`` registered.
    """
    components = components or {}
    registry = StepExecutorRegistry()
    registry.register(
        DltStepExecutor(
            components=components,
            run_fn=dlt_run_fn,
            timeout_seconds=dlt_timeout_seconds,
        )
    )
    registry.register(
        DbtStepExecutor(
            dbt_executable=dbt_executable,
            components=components,
            backend_factory=dbt_backend_factory,
            timeout_seconds=dbt_timeout_seconds,
        )
    )
    registry.register(
        SqlStepExecutor(
            connections=connections,
            connection_factory=connection_factory,
            row_cap=sql_row_cap,
            timeout_seconds=sql_timeout_seconds,
        )
    )
    return registry


__all__ = ["build_step_executor_registry"]
