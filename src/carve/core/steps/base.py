"""Step protocol shared by every step type.

The protocol is intentionally minimal: a step is little more than a
typed config holder. Actual execution happens in a `Runner`, which
inspects `step.config` to drive the subprocess (or HTTP call, or SQL
statement, depending on the runner). Keeping the protocol minimal lets
M3 add SQL/dbt/shell/http step types without touching this file.

`StepResult` is what a runner's `wait()` returns. `RunContext` is the
bundle of run-scoped state — id, project root, target, full config —
that runners need to do their job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from carve.core.config import Config


class StepConfig(BaseModel):
    """Base for all step type configs.

    Concrete step types subclass this and add type-specific fields. The
    fields here apply to every step: a unique id, a wall-clock timeout,
    and retry knobs. Retries aren't wired up in M1 — they exist on the
    schema so M3 can implement them without breaking config files.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    timeout_seconds: int = 1800
    retries: int = 0
    retry_backoff_seconds: int = 60


class StepResult(BaseModel):
    """Outcome of a single step execution.

    `status` is one of ``"success"``, ``"failed"``, ``"cancelled"``.
    `outputs` is a free-form dict reserved for M3's step-output passing;
    M1 leaves it empty.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    duration_ms: int
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RunContext(BaseModel):
    """Run-scoped state passed to a runner alongside a step.

    `project_dir` is the resolved root of the user's Carve project
    (where ``carve.toml`` lives). `target` is the active connection
    target (e.g. ``"dev"``). `config` is the full merged `Config` so the
    runner can pull connection credentials without us threading them
    through a separate parameter.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    project_dir: Path
    target: str
    config: Config


@runtime_checkable
class Step(Protocol):
    """Every step type implements this.

    The protocol is structural — runners only care about reading
    ``step.config`` and ``step.step_type``. The `validate` classmethod
    parses a TOML-derived dict into the concrete config subclass; M2's
    pipeline loader will call it.
    """

    config: StepConfig

    @property
    def step_type(self) -> str:
        """The string used in TOML's ``type = "..."`` field."""
        ...

    def validate(self, config_dict: dict[str, Any]) -> StepConfig:
        """Validate the config from TOML. Returns parsed config."""
        ...
