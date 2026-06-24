"""dbt execution backends — run dbt behind one backend-uniform interface.

This package owns *how a dbt component is executed* and *which engine runs it*.
A component runs through a :class:`DbtBackend` (the ``local`` subprocess backend
here; managed backends later), is invoked with a typed :class:`DbtCommand`, and
returns a backend-uniform :class:`DbtRunResult` — the ``dbt`` step type and the
dbt-engineer agent loop never branch on which backend ran the build.

The *result-parsing* layer lives in the already-shipped
``carve.integrations.dbt`` (``read_run_results`` / ``DbtRunReport``) and is
*imported*, never re-implemented; this package adds the subprocess that produces
``target/`` and the engine resolve/pin that makes the choice reproducible.
"""

from __future__ import annotations

from carve.core.dbt_execution.backend import DbtBackend, DbtCommand
from carve.core.dbt_execution.engine import (
    ENGINE_DBT_CORE,
    ENGINE_FUSION,
    EnginePin,
    pin_engine,
    resolve_engine,
    resolve_or_reuse,
)
from carve.core.dbt_execution.local import (
    ENV_BUNDLED,
    ENV_EXTERNAL,
    LocalDbtBackend,
    UnsupportedBackendError,
    build_backend,
)
from carve.core.dbt_execution.result import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_SUCCESS,
    DbtRunResult,
    PerModelResult,
)

__all__ = [
    "ENGINE_DBT_CORE",
    "ENGINE_FUSION",
    "ENV_BUNDLED",
    "ENV_EXTERNAL",
    "STATUS_ERROR",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "DbtBackend",
    "DbtCommand",
    "DbtRunResult",
    "EnginePin",
    "LocalDbtBackend",
    "PerModelResult",
    "UnsupportedBackendError",
    "build_backend",
    "pin_engine",
    "resolve_engine",
    "resolve_or_reuse",
]
