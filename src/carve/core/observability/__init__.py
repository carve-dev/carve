"""Carve's observability substrate — agent/skill recording + metrics rollups.

Two surfaces live here:

* :class:`~carve.core.observability.recording.RecordingObserver` — the
  ``AgentObserver`` the delegation call-site wires onto a subagent run so each
  invocation + tool call is persisted (best-effort) into the agent-telemetry
  tables via :class:`~carve.core.state.telemetry.TelemetryRepo`.
* :class:`~carve.core.observability.rollups.MetricsRollups` — the DB-backed
  aggregation behind ``carve metrics costs|runs|agents`` (and, in Increment 5,
  the ``GET /metrics/*`` routers that wire onto the same service).

OpenTelemetry/OTLP export is deferred to a follow-up ``otel`` slice.
"""

from __future__ import annotations

from carve.core.observability.recording import RecordingObserver
from carve.core.observability.rollups import (
    AgentUsage,
    CostsRollup,
    MetricsRollups,
    RunsRollup,
    TargetRuns,
    parse_since,
)

__all__ = [
    "AgentUsage",
    "CostsRollup",
    "MetricsRollups",
    "RecordingObserver",
    "RunsRollup",
    "TargetRuns",
    "parse_since",
]
