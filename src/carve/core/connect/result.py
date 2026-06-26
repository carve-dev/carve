"""Typed results + errors for the connect provision loop.

These are the small value objects the provision loop returns and raises. They
keep the loop's contract explicit — *what was resolved / installed / pinned /
validated*, or *why nothing happened* — so callers (the `carve connect` command
and the orchestrator's first-use trigger) branch on a typed outcome rather than
re-deriving state from the config.

The error hierarchy is rooted at :class:`ConnectError` so a caller can catch the
whole family with one `except`. The two load-bearing ones:

* :class:`EngineInstallNotSupported` — the **deferred** half of the installer.
  The Fusion (Apache-2.0 dbt Core v2.0) **binary fetch** is not built in this
  slice; resolution still *pins* fusion correctly, but installing it raises this
  so the deferral can never silently false-succeed.
* :class:`ValidationFailed` — the installed engine failed its post-install
  validate (`dbt --version` / `dbt parse`). It is raised **before** any pin is
  written, which is what makes the loop fail-closed (see ``dbt_provision``).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from carve.core.dbt_execution.engine import EnginePin


class ProvisionOutcome(StrEnum):
    """What the provision loop did, as one discrete string.

    * ``provisioned`` — resolved → installed → validated → pinned (the first-use
      path).
    * ``noop`` — already pinned **and** the engine is present on disk; nothing
      to do (the idempotent second-run path).
    * ``managed`` — a managed backend (``snowflake-native`` / ``dbt-cloud`` /
      ``remote``); refs/creds are wired and **no** engine is installed.
    * ``external`` — ``dbt_env == "external"``; the user's own dbt at
      ``dbt_path`` is used and **nothing** is installed.
    """

    PROVISIONED = "provisioned"
    NOOP = "noop"
    MANAGED = "managed"
    EXTERNAL = "external"


class InstalledEngine(BaseModel):
    """An engine the installer materialized on disk.

    ``executable`` is the engine binary (argv[0]) a caller injects as
    ``LocalDbtBackend(dbt_executable=…)`` — the precise value
    :func:`carve.core.connect.installer.install_engine` returns.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: str
    version: str
    executable: Path


class ProvisionResult(BaseModel):
    """The typed result of one :func:`provision_dbt_engine` call.

    ``pin`` / ``engine_path`` / ``validated`` are populated only on the
    ``provisioned`` and ``noop`` paths (a pin exists). ``managed`` / ``external``
    short-circuits leave them ``None`` / ``False`` — there is no engine and no
    pin.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: ProvisionOutcome
    component_name: str
    pin: EnginePin | None = None
    engine_path: str | None = None
    validated: bool = False


class ConnectError(Exception):
    """Root of the connect error family — catch this to catch them all."""


class EngineInstallNotSupported(ConnectError):
    """The chosen engine's install path is not implemented in this slice.

    Raised by the installer for the ``fusion`` engine: resolution pins it
    correctly (so the config is right), but the Apache-2.0 dbt Core v2.0 **Rust
    binary** fetch is deferred. A clear, typed error (asserted by a test) keeps
    the deferral honest — it can never read as a silent success.
    """


class ValidationFailed(ConnectError):
    """The installed engine failed its post-install validate.

    Raised by the validate step (`dbt --version` / `dbt parse`) **before** any
    pin is written. Reaching this means the on-disk config is untouched — the
    fail-closed ordering invariant (see ``dbt_provision``).
    """


class UnsafeEnginePath(ConnectError):
    """The keyed engine-install dir escapes ``install_root``.

    Defense-in-depth at the install sink: ``dbt_version`` is validated at the
    config-load boundary (``ComponentConfig._safe_dbt_version``), but the
    installer keys a venv dir on ``<engine>-<version>`` that it then ``mkdir``s
    and execs (`<dir>/bin/dbt`). This guards the sink directly so a
    directly-constructed ``EnginePin`` carrying a traversal version can never
    resolve to a path outside ``install_root`` — closed regardless of caller.
    """


__all__ = [
    "ConnectError",
    "EngineInstallNotSupported",
    "InstalledEngine",
    "ProvisionOutcome",
    "ProvisionResult",
    "UnsafeEnginePath",
    "ValidationFailed",
]
