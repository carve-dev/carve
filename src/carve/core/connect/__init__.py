"""connect — on-demand provisioning of the things a pipeline reaches for.

This package is connect's lean first slice: **lazy bundled-dbt-engine
provisioning + pin**. It builds the *deferred half* of `engine.py` (which
decides + pins but explicitly does **not** install) — the installer plus the
provision loop that drives resolve → install → validate → pin on first use.

The loop honors two load-bearing invariants (see ``dbt_provision``): it is
**fail-closed** (a failed validate writes no config — the pin is physically
unreachable until validate succeeds) and **idempotent on two checks** (a second
run is a no-op only when the component is pinned **and** the engine is present on
disk; a pin without an install re-installs).

Fenced out of this slice: warehouse/source connect, credential capture
(``carve env set``), the Fusion binary fetch (resolution pins it; install raises
:class:`EngineInstallNotSupported`), and the full mid-task implicit orchestrator
trigger (only a thin importable seam — :func:`provision_dbt_engine` — ships).
"""

from __future__ import annotations

from carve.core.connect.dbt_provision import (
    Installer,
    Validator,
    provision_dbt_engine,
)
from carve.core.connect.installer import (
    engine_executable,
    engine_install_dir,
    install_engine,
)
from carve.core.connect.result import (
    ConnectError,
    EngineInstallNotSupported,
    InstalledEngine,
    ProvisionOutcome,
    ProvisionResult,
    ValidationFailed,
)

__all__ = [
    "ConnectError",
    "EngineInstallNotSupported",
    "InstalledEngine",
    "Installer",
    "ProvisionOutcome",
    "ProvisionResult",
    "ValidationFailed",
    "Validator",
    "engine_executable",
    "engine_install_dir",
    "install_engine",
    "provision_dbt_engine",
]
