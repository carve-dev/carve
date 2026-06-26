"""The bundled-engine installer — connect's deferred half of `engine.py`.

`engine.py` *decides + pins*; this module *installs*. It is the half its module
docstring names: "the **install** of the chosen engine is connect's deferred
half (the engine binary path is injected into the backend, never installed
here)." :func:`install_engine` materializes the engine and returns the binary
path a caller injects as ``LocalDbtBackend(dbt_executable=…)``.

Install mechanism (the one real design call this slice makes):

* **dbt-core** → a **Carve-managed venv** under ``install_root``, with
  ``dbt-core==<version>`` + the dialect's adapter (``dbt-duckdb`` for duckdb,
  ``dbt-snowflake`` for snowflake, …) pip-installed into it, returning the
  venv's ``bin/dbt``. The venv is keyed by ``<engine>-<version>`` so the same
  pin reuses the same venv and "engine present on disk" is a deterministic path
  check (see :func:`engine_install_dir` — the loop's idempotence half reads it).
* **fusion** → **DEFERRED**. ``ENGINE_FUSION`` is the Apache-2.0 dbt Core v2.0
  **Rust binary** (NOT pip, NOT ELv2 — see ``engine.py``'s license note); its
  binary fetch is not built in this slice. Resolution still pins fusion
  correctly; installing it raises :class:`EngineInstallNotSupported` so the
  deferral can never silently false-succeed.

The process work rides the shipped subprocess discipline
(:class:`carve.core.runners.subprocess.Subprocess`) — own process group,
secret-stripped child env, wall-clock watchdog — never hand-rolled. The
**``runner`` seam** is the offline-test injection point: a fake runner records
the argv and touches a fake ``bin/dbt`` instead of hitting PyPI, so the install
path is exercised end-to-end with no real dbt and no network.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from carve.core.connect.result import (
    EngineInstallNotSupported,
    InstalledEngine,
    UnsafeEnginePath,
)
from carve.core.dbt_execution.engine import ENGINE_DBT_CORE, ENGINE_FUSION, EnginePin
from carve.core.runners.subprocess import Subprocess

# Per-dialect dbt adapter package. dbt-core needs the warehouse adapter installed
# alongside it; the dialect (the `sql` dialect axis) selects which one.
_ADAPTER_PACKAGES = {
    "duckdb": "dbt-duckdb",
    "snowflake": "dbt-snowflake",
    "postgres": "dbt-postgres",
    "bigquery": "dbt-bigquery",
    "databricks": "dbt-databricks",
    "redshift": "dbt-redshift",
}

# Wall-clock budget for the venv-create + pip-install subprocesses. A pip install
# of the dbt adapter stack can be slow on a cold cache; generous but bounded so a
# wedged network can't hang a worker forever.
_INSTALL_TIMEOUT_SECONDS = 1800

# An install step: (argv, cwd) -> None. Spawns one subprocess to completion and
# raises on a non-zero exit. The default rides `Subprocess`; tests inject a fake.
InstallRunner = Callable[[list[str], Path], None]


def _default_runner(argv: list[str], cwd: Path) -> None:
    """Run one install subprocess to completion, raising on a non-zero exit.

    Rides :meth:`Subprocess.run_to_completion` for the shipped discipline (own
    process group, stripped secrets, watchdog). A non-zero exit becomes a
    :class:`RuntimeError` carrying the captured output so a pip failure is
    legible rather than a bare returncode.
    """
    completed = Subprocess.run_to_completion(
        argv,
        cwd=cwd,
        timeout_seconds=_INSTALL_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"install step failed (exit {completed.returncode}): "
            f"{' '.join(argv)}\n{completed.output}"
        )


def engine_install_dir(pin: EnginePin, *, install_root: Path) -> Path:
    """The deterministic venv dir for ``pin`` under ``install_root``.

    Keyed by ``<engine>-<version>`` so the same pin always maps to the same
    venv — the install is idempotent (a present venv is reused) and the loop's
    "engine present on disk" check (:func:`engine_executable`) is a pure path
    test against this dir.

    Defense-in-depth at the install sink: ``dbt_version`` is validated at the
    config-load boundary, but this dir is ``mkdir``'d and execs ``bin/dbt``, so
    confirm the keyed dir stays WITHIN ``install_root`` regardless of how the
    ``EnginePin`` was constructed — a traversal version can never escape.
    """
    candidate = install_root / f"{pin.dbt_engine}-{pin.dbt_version}"
    root = install_root.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise UnsafeEnginePath(
            f"engine install dir {candidate} (from version {pin.dbt_version!r}) "
            f"escapes install_root {install_root}"
        )
    return candidate


def engine_executable(venv_dir: Path) -> Path:
    """The dbt binary inside a managed venv (POSIX or Windows)."""
    if os.name == "nt":  # pragma: no cover - tests run on POSIX
        return venv_dir / "Scripts" / "dbt.exe"
    return venv_dir / "bin" / "dbt"


def _venv_python(venv_dir: Path) -> Path:
    """The python interpreter inside a managed venv (POSIX or Windows)."""
    if os.name == "nt":  # pragma: no cover - tests run on POSIX
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _adapter_package(dialect: str, version: str) -> str:
    """``dbt-<adapter>==<version>`` for ``dialect`` (pinned to the engine version).

    The adapter is pinned to the same ``version`` as ``dbt-core`` so the
    reproducibility anchor covers the whole stack, not just the core. An
    unrecognized dialect falls back to ``dbt-core``'s own dialect set raising a
    clear error rather than guessing a package name.
    """
    name = dialect.strip().lower()
    package = _ADAPTER_PACKAGES.get(name)
    if package is None:
        raise EngineInstallNotSupported(
            f"no dbt adapter mapping for dialect {dialect!r}; "
            f"supported: {', '.join(sorted(_ADAPTER_PACKAGES))}."
        )
    return f"{package}=={version}"


def install_engine(
    pin: EnginePin,
    dialect: str,
    *,
    install_root: Path,
    python_executable: str | None = None,
    runner: InstallRunner = _default_runner,
) -> InstalledEngine:
    """Install the engine ``pin`` names and return its binary path.

    For ``dbt-core``: create (or reuse) a Carve-managed venv under
    ``install_root`` keyed by ``<engine>-<version>``, ``pip install
    dbt-core==<version>`` + the dialect adapter into it, and return the venv's
    ``bin/dbt`` as :class:`InstalledEngine`. The returned ``executable`` is
    exactly what a caller passes as ``LocalDbtBackend(dbt_executable=…)``.

    For ``fusion``: raise :class:`EngineInstallNotSupported` — the Apache-2.0
    dbt Core v2.0 Rust-binary fetch is deferred this slice (resolution still
    pins it correctly).

    ``runner`` is the install seam: each ``pip``/``venv`` invocation goes through
    it. The default runs real subprocesses via :class:`Subprocess`; tests inject
    a fake that records argv and touches a fake binary (offline, no PyPI).
    """
    if pin.dbt_engine == ENGINE_FUSION:
        raise EngineInstallNotSupported(
            "fusion engine install is not yet implemented (the Apache-2.0 dbt "
            "Core v2.0 Rust binary is fetched, not pip-installed). Resolution "
            "still pins fusion correctly; for a runnable bundled engine today "
            "use a dbt-core dialect (e.g. DuckDB)."
        )
    if pin.dbt_engine != ENGINE_DBT_CORE:  # pragma: no cover - schema-constrained
        raise EngineInstallNotSupported(f"unknown engine {pin.dbt_engine!r}; cannot install.")

    venv_dir = engine_install_dir(pin, install_root=install_root)
    dbt_bin = engine_executable(venv_dir)

    # Reuse an already-materialized venv (idempotent install): if the binary is
    # already there, don't re-create or re-pip. The loop's own present-on-disk
    # check fronts this, but the installer staying idempotent is belt-and-braces.
    if not dbt_bin.is_file():
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        interpreter = python_executable or _interpreter()
        runner([interpreter, "-m", "venv", str(venv_dir)], install_root)
        pip = str(_venv_python(venv_dir))
        # `--` terminates pip flag parsing so a version spec can never be read as
        # a flag; the packages are built from the validated pin + the dialect map.
        runner(
            [
                pip,
                "-m",
                "pip",
                "install",
                "--",
                f"dbt-core=={pin.dbt_version}",
                _adapter_package(dialect, pin.dbt_version),
            ],
            install_root,
        )

    return InstalledEngine(
        engine=pin.dbt_engine,
        version=pin.dbt_version,
        executable=dbt_bin,
    )


def _interpreter() -> str:
    """The interpreter used to create managed venvs (the running python)."""
    import sys

    return sys.executable


__all__ = [
    "InstallRunner",
    "engine_executable",
    "engine_install_dir",
    "install_engine",
]
