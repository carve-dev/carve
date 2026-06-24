"""The backend-agnostic dbt execution interface.

A dbt component is run through *some* backend — ``local`` (a dbt-core/Fusion
subprocess, this slice) or, later, a managed backend (snowflake-native /
dbt-cloud / remote). The ``dbt`` step type (deferred) and the dbt-engineer agent
loop call :meth:`DbtBackend.run` and never branch on which backend they hold:
each backend returns the same backend-uniform
:class:`~carve.core.dbt_execution.result.DbtRunResult`.

:class:`DbtCommand` is the one typed invocation object ``run`` takes, so the
arg-flattening (``build`` → ``["build", "--select", …, "--target", …]``) lives
in exactly one tested place rather than being re-derived per backend.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from carve.core.dbt_execution.result import DbtRunResult

# The dbt subcommands a Carve component invocation supports.
_DBT_COMMANDS = frozenset({"build", "run", "test", "snapshot", "seed"})
# dbt rejects --full-refresh on these subcommands (it applies to model/seed
# materialization, not to test or snapshot runs).
_NO_FULL_REFRESH_COMMANDS = frozenset({"test", "snapshot"})


class DbtCommand(BaseModel):
    """A normalized dbt invocation, independent of backend.

    ``command`` is the dbt subcommand (``build``/``run``/``test``/``snapshot``/
    ``seed``). ``select`` / ``exclude`` are node selectors, ``vars`` are dbt
    ``--vars`` (passed as a YAML/JSON mapping), ``target`` is the profiles
    target, ``full_refresh`` toggles ``--full-refresh``.
    """

    model_config = ConfigDict(extra="forbid")

    command: str
    select: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    vars: Mapping[str, object] | None = None
    target: str | None = None
    full_refresh: bool = False

    @field_validator("command")
    @classmethod
    def _known_command(cls, value: str) -> str:
        if value not in _DBT_COMMANDS:
            raise ValueError(f"dbt command must be one of {sorted(_DBT_COMMANDS)}; got {value!r}")
        return value

    @field_validator("select", "exclude", mode="before")
    @classmethod
    def _coerce_selectors(cls, value: object) -> object:
        # Accept a single string selector or an iterable; normalize to a tuple
        # so the flattener is uniform. Reject option-shaped selectors that
        # would be parsed as dbt flags rather than node selectors.
        if value is None:
            return ()
        if isinstance(value, str):
            items: tuple[str, ...] = (value,)
        elif isinstance(value, (list, tuple)):
            items = tuple(str(v) for v in value)
        else:
            return value  # let pydantic raise a precise type error
        for item in items:
            if item.startswith("-"):
                raise ValueError(f"selector must not start with '-' (option-shaped); got {item!r}")
        return items

    @field_validator("target")
    @classmethod
    def _safe_target(cls, value: str | None) -> str | None:
        # The target binds as the argument to --target, but reject an
        # option-shaped value defensively (consistent with select/exclude) so an
        # agent-authored target can't smuggle a flag when this is wired live.
        if value is not None and value.startswith("-"):
            raise ValueError(f"target must not start with '-' (option-shaped); got {value!r}")
        return value

    @model_validator(mode="after")
    def _full_refresh_supported(self) -> DbtCommand:
        if self.full_refresh and self.command in _NO_FULL_REFRESH_COMMANDS:
            raise ValueError(f"--full-refresh is not valid for dbt {self.command}")
        return self

    def to_argv(self) -> list[str]:
        """Flatten into the dbt subprocess argv (excluding the executable).

        ``build --select a b --exclude c --target dev --full-refresh
        --vars '{"k": "v"}'``. The executable (engine binary) and
        ``--project-dir``/``--profiles-dir`` are prepended by the backend that
        knows the resolved paths — this method owns only the command + flags.
        """
        argv: list[str] = [self.command]
        if self.select:
            argv += ["--select", *self.select]
        if self.exclude:
            argv += ["--exclude", *self.exclude]
        if self.target:
            argv += ["--target", self.target]
        if self.full_refresh:
            argv.append("--full-refresh")
        if self.vars:
            argv += ["--vars", json.dumps(dict(self.vars), separators=(",", ":"))]
        return argv


@runtime_checkable
class DbtBackend(Protocol):
    """Every dbt-execution backend implements this one method.

    Structural (``@runtime_checkable``), mirroring
    :class:`carve.core.steps.base.Step`: the caller only needs ``run`` and the
    uniform :class:`DbtRunResult` it returns. The managed backends that complete
    the family satisfy the same contract.
    """

    def run(self, command: DbtCommand) -> DbtRunResult:
        """Execute ``command`` and return the backend-uniform result."""
        ...


__all__ = ["DbtBackend", "DbtCommand"]
