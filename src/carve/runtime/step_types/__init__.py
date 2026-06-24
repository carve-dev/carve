"""The three concrete step executors + the registry-builder.

This package adds the ``dlt``/``dbt``/``sql`` executors that implement the
Unit-1 :class:`~carve.runtime.step_executor.StepExecutor` seam, plus
:func:`~carve.runtime.step_types.registry.build_step_executor_registry`, which
wires all three (with their injectable seams) into a
:class:`~carve.runtime.step_executor.StepExecutorRegistry` that
``execute_pipeline`` drives. Each executor *calls* an already-shipped backend
(the dlt load-package parser + venv/subprocess primitive, the ``LocalDbtBackend``,
the ``sql``/connectors layer) — this is composition plumbing, not authoring.
"""

from __future__ import annotations

from carve.runtime.step_types.connections import (
    Connection,
    ConnectionResolutionError,
    resolve_connection,
)
from carve.runtime.step_types.dbt import DbtStepExecutor
from carve.runtime.step_types.dlt import DltStepExecutor
from carve.runtime.step_types.registry import build_step_executor_registry
from carve.runtime.step_types.sql import SqlStepExecutor

__all__ = [
    "Connection",
    "ConnectionResolutionError",
    "DbtStepExecutor",
    "DltStepExecutor",
    "SqlStepExecutor",
    "build_step_executor_registry",
    "resolve_connection",
]
