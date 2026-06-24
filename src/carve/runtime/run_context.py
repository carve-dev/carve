"""The run-context value object the composition core walks against.

The spec's ``execute_pipeline(run: Run, ...)`` takes the runtime's ``Run``
row. That row (and its table) is **runtime's** (Increment 4); this unit
must not depend on it. So the composition core walks against a small,
self-contained :class:`PipelineRun` value object carrying exactly the
fields this unit reads — the pipeline name (to load the TOML), the target,
the trigger, the run id, and the start time (surfaced in the Jinja ``run``
namespace).

When the Increment-4 runtime lands its ``Run`` row, it either constructs a
``PipelineRun`` from it at the ``execute_pipeline`` boundary or makes the
row satisfy this shape — either way ``execute_pipeline``'s signature is
unaffected. Kept a frozen dataclass (not a pydantic model): it is internal
plumbing, never parsed from user input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4


@dataclass(frozen=True)
class PipelineRun:
    """One pipeline run's identity + dispatch context.

    * ``pipeline`` — the pipeline name; ``execute_pipeline`` loads
      ``pipelines/<pipeline>.toml``.
    * ``target`` — the connection target the run executes against.
    * ``trigger`` — what started the run (``"scheduled"``/``"manual"``/…);
      surfaced in the Jinja ``run`` namespace, not interpreted here.
    * ``id`` — the run id (a uuid4 hex string by default).
    * ``started_at`` — run start time (UTC by default).
    """

    pipeline: str
    target: str = "prod"
    trigger: str = "manual"
    id: str = field(default_factory=lambda: uuid4().hex)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


__all__ = ["PipelineRun"]
