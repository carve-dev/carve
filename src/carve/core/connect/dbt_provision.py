"""The connect provision loop — resolve → install → validate → pin, fail-closed.

This is connect's lazy first-use path for the bundled dbt engine. It drives the
shipped `engine.py` decision/pin functions and this package's installer through
a loop with two **load-bearing invariants**:

1. **Fail-closed is an ORDERING invariant.** ``pin_engine`` is the *last*
   statement and is reached *only after* ``validate`` returns success. A failed
   validate raises before any write, so a broken engine is never pinned and the
   on-disk ``carve.toml`` is left byte-identical. This is structural, not a
   try/except afterthought — read the order top-to-bottom in
   :func:`provision_dbt_engine`.

2. **Idempotence is TWO checks.** The no-op path requires the component to be
   pinned **AND** the engine present on disk. A pin alone is not "done": if the
   managed venv was wiped, a pin-without-install **re-installs**. The
   "pinned-and-reused-not-re-resolved" half rides ``resolve_or_reuse``; the
   present-on-disk half is :func:`carve.core.connect.installer.engine_executable`.

Two short-circuits never install an engine:

* **Managed backend** (``snowflake-native`` / ``dbt-cloud`` / ``remote`` — the
  same set ``local.py`` rejects at backend construction): refs/creds are wired
  elsewhere; connect returns a ``managed`` result and never calls the installer.
* **External dbt** (``dbt_env == "external"``): the user's own dbt at
  ``dbt_path`` is used; connect returns an ``external`` result and installs
  nothing.

Every external effect — install, validate — is an **injected seam** so the loop
runs fully offline in tests with a fake installer and a fake ``dbt --version``,
the same injected-engine discipline the dbt-execution backend tests use.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from carve.core.config.schema import ComponentConfig
from carve.core.connect.installer import (
    engine_executable,
    engine_install_dir,
    install_engine,
)
from carve.core.connect.result import (
    InstalledEngine,
    ProvisionOutcome,
    ProvisionResult,
    ValidationFailed,
)
from carve.core.dbt_execution.engine import EnginePin, pin_engine, resolve_or_reuse
from carve.core.dbt_execution.local import ENV_EXTERNAL
from carve.core.runners.subprocess import Subprocess

# The managed backends connect must NOT install for — the exact set `local.py`
# raises `UnsupportedBackendError` on at backend construction. Derived from the
# same `backend != "local"` boundary so the membership test isn't duplicated.
_MANAGED_BACKENDS = frozenset({"snowflake-native", "dbt-cloud", "remote"})

# An installer: (pin, dialect) -> InstalledEngine. The default binds the package
# installer to ``install_root``; tests inject a fake that touches a fake binary.
Installer = Callable[[EnginePin, str], InstalledEngine]

# A validator: (engine) -> None on success, raises `ValidationFailed` otherwise.
# The default runs ``dbt --version`` through `Subprocess`; tests inject a fake.
Validator = Callable[[InstalledEngine], None]

_VALIDATE_TIMEOUT_SECONDS = 120


def _default_validate(engine: InstalledEngine) -> None:
    """Validate an installed engine by running ``dbt --version`` through it.

    A clean exit (returncode 0) is the pass. Any non-zero exit — or the binary
    not being executable — raises :class:`ValidationFailed`, which (by the loop's
    ordering) is raised *before* any pin is written.
    """
    completed = Subprocess.run_to_completion(
        [str(engine.executable), "--version"],
        cwd=engine.executable.parent,
        timeout_seconds=_VALIDATE_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise ValidationFailed(
            f"engine validate failed (`dbt --version` exit {completed.returncode}): "
            f"{completed.output.strip()[:500]}"
        )


def _make_default_installer(*, install_root: Path) -> Installer:
    """Bind the package installer to ``install_root`` as a 2-arg `Installer`."""

    def _install(pin: EnginePin, dialect: str) -> InstalledEngine:
        return install_engine(pin, dialect, install_root=install_root)

    return _install


def provision_dbt_engine(
    component: ComponentConfig,
    *,
    component_name: str,
    dialect: str,
    config_path: Path,
    install_root: Path,
    install: Installer | None = None,
    validate: Validator | None = None,
) -> ProvisionResult:
    """Provision (resolve → install → validate → pin) the bundled dbt engine.

    The lazy first-use path: resolve the right engine for ``dialect``, install
    it, validate the install, then pin the choice into ``[components.<name>]`` of
    ``config_path``. Idempotent (a second run is a ``noop``) and fail-closed (a
    failed validate writes nothing).

    Short-circuits — never installs: a **managed** backend
    (``snowflake-native``/``dbt-cloud``/``remote``) returns ``managed``; an
    **external** dbt (``dbt_env == "external"``) returns ``external``.

    ``install`` / ``validate`` are injected seams (default to the package
    installer + ``dbt --version``) so the loop runs offline in tests with no real
    dbt.
    """
    # --- short-circuit 1: managed backend → wire only, install nothing --------
    if component.dbt_backend in _MANAGED_BACKENDS:
        return ProvisionResult(
            outcome=ProvisionOutcome.MANAGED,
            component_name=component_name,
        )

    # --- short-circuit 2: external dbt → use the user's dbt, install nothing ---
    if component.dbt_env == ENV_EXTERNAL:
        return ProvisionResult(
            outcome=ProvisionOutcome.EXTERNAL,
            component_name=component_name,
        )

    installer = (
        install if install is not None else _make_default_installer(install_root=install_root)
    )
    # Both seams resolve to their defaults at *call* time (not as bound default
    # args) so a test can monkeypatch `install_engine` / `_default_validate` on
    # this module and have the loop pick the patched version up.
    validator = validate if validate is not None else _default_validate

    # Resolve the engine: an existing pin is reused, not re-resolved (the
    # "pinned-and-reused-not-re-resolved" invariant from `resolve_or_reuse`).
    pin = resolve_or_reuse(component, dialect)
    already_pinned = component.dbt_engine is not None and component.dbt_version is not None

    # --- idempotence: TWO checks (pinned AND present-on-disk) ------------------
    # A pin alone is NOT "done": if the managed venv was wiped, a pinned-but-
    # missing engine must RE-INSTALL. Only pinned + present is a no-op.
    if already_pinned and _engine_present_on_disk(pin, install_root=install_root):
        return ProvisionResult(
            outcome=ProvisionOutcome.NOOP,
            component_name=component_name,
            pin=pin,
            engine_path=str(engine_executable(engine_install_dir(pin, install_root=install_root))),
            validated=False,
        )

    # --- the fail-closed ordering: install → validate → (ONLY THEN) pin -------
    # `install_engine` may raise `EngineInstallNotSupported` (fusion deferred) —
    # it propagates; nothing is pinned.
    installed = installer(pin, dialect)

    # `validator` raises `ValidationFailed` on a bad engine. Because it is *above*
    # `pin_engine`, a failed validate leaves `config_path` byte-identical — no
    # partial config, no broken pin. This ordering IS the fail-closed guarantee.
    validator(installed)

    # Reached only after a successful validate. This is the single write.
    pin_engine(component_name, pin, config_path=config_path)

    return ProvisionResult(
        outcome=ProvisionOutcome.PROVISIONED,
        component_name=component_name,
        pin=pin,
        engine_path=str(installed.executable),
        validated=True,
    )


def _engine_present_on_disk(pin: EnginePin, *, install_root: Path) -> bool:
    """True iff the engine ``pin`` names is materialized under ``install_root``.

    The present-on-disk half of the two-check idempotence: a deterministic path
    test against the venv the installer would create (and reuse) for this pin.
    """
    return engine_executable(engine_install_dir(pin, install_root=install_root)).is_file()


__all__ = [
    "Installer",
    "Validator",
    "provision_dbt_engine",
]
