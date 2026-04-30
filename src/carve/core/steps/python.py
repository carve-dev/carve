"""The M1 Python step type.

A `PythonStep` is a pointer at a script on disk plus a list of pip
requirements and any extra environment variables to inject. The runner
(`LocalVenvRunner`) does the actual work of materialising a venv,
spawning the subprocess, and streaming logs.

The step itself is a passive config holder — keeping the logic in the
runner means M3's future runner types (e.g. a Docker runner) can reuse
the same `PythonStep` objects without modification.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, field_validator

from carve.core.steps.base import StepConfig


class PythonStepConfig(StepConfig):
    """Typed config for ``type = "python"`` steps.

    `script` is interpreted relative to the project root (the directory
    containing ``carve.toml``); the runner enforces that the resolved
    path stays inside the project. `requirements` is a list of pip
    spec strings — same syntax as ``pip install``. `env` is merged on
    top of the runner's base environment when the subprocess is spawned.
    """

    model_config = ConfigDict(extra="forbid")

    script: str
    requirements: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("requirements")
    @classmethod
    def _reject_pip_flags(cls, value: list[str]) -> list[str]:
        """Reject requirement entries that look like pip flags.

        A leading ``-`` would let an entry like ``--index-url=...`` or
        ``-r /tmp/x.txt`` redirect pip away from PyPI when the runner
        materialises the venv. The runner also passes ``--`` to pip as
        a defence in depth, but we fail fast here with a clear error so
        misconfiguration is obvious at config-load time.
        """
        for entry in value:
            if entry.startswith("-"):
                raise ValueError(
                    f"requirements entries must be package specs, "
                    f"not flags; got {entry!r}"
                )
        return value


class PythonStep:
    """The Python step type.

    Holds a `PythonStepConfig`. The runner reads ``self.config`` to
    decide what to execute; nothing else.
    """

    step_type: str = "python"

    def __init__(self, config: PythonStepConfig) -> None:
        self.config: PythonStepConfig = config

    def validate(self, config_dict: dict[str, Any]) -> PythonStepConfig:
        """Parse a TOML-derived dict into a `PythonStepConfig`."""
        return PythonStepConfig.model_validate(config_dict)
