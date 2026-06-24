"""The ``local`` dbt-execution backend — a dbt-core/Fusion subprocess.

``LocalDbtBackend`` runs ``<engine_bin> <command> --select … --target …`` as a
**subprocess, always** — never the in-process ``dbtRunner``. This is a
*correctness* requirement, not a style choice: dbt cannot safely run concurrent
invocations in one process, and Carve's worker pool is concurrent, so two
concurrent ``run`` calls must land in two separate OS processes.

The subprocess discipline mirrors
:class:`carve.core.runners.local_venv.LocalVenvRunner` exactly, factored into the
shared :class:`carve.core.runners.subprocess.Subprocess` primitive:

* own process group (``start_new_session=True``) so cancellation signals the
  whole tree (a dbt run can fork its adapter's children);
* SIGTERM → grace → SIGKILL cancellation;
* a wall-clock watchdog timeout;
* the Carve-internal secret env vars (``ANTHROPIC_API_KEY`` /
  ``ANTHROPIC_AUTH_TOKEN``) **stripped** from the child env — a dbt run may
  execute LLM-authored profiles/macros and must never see Carve's API key.

The **engine binary path is injected** (``dbt_executable``). This (a) lets tests
run the backend's orchestration with a fake engine and no real dbt installed,
and (b) keeps connect's lazy *install* genuinely deferred — this slice decides +
pins + invokes; connect installs and populates the injected path.

Result normalization rides the **shipped** substrate
(:func:`carve.integrations.dbt.verify.read_run_results` → ``target/run_results.json``
+ ``target/manifest.json``) and is fail-closed: a clean exit with no readable
artifact is **not** green.
"""

from __future__ import annotations

import time
from pathlib import Path

from carve.core.dbt_execution.backend import DbtCommand
from carve.core.dbt_execution.result import DbtRunResult
from carve.core.runners.subprocess import Subprocess
from carve.integrations.dbt.verify import read_run_results

# dbt envs: `bundled` = Carve-managed engine; `external` = user-installed dbt.
ENV_BUNDLED = "bundled"
ENV_EXTERNAL = "external"

_DEFAULT_TIMEOUT_SECONDS = 1800


class LocalDbtBackend:
    """Run dbt as a subprocess against a resolved project dir.

    ``dbt_executable`` is the resolved engine binary (argv[0]) — injected, not
    installed by this backend. ``project_dir`` is the resolved dbt project
    directory (root or one-level-down — resolution rides the shipped locator at
    the caller). ``profiles_dir`` is passed through for ``env == "external"``.
    """

    def __init__(
        self,
        *,
        dbt_executable: str | Path,
        project_dir: str | Path,
        env: str = ENV_BUNDLED,
        profiles_dir: str | Path | None = None,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.dbt_executable = str(dbt_executable)
        self.project_dir = Path(project_dir).resolve()
        if env not in (ENV_BUNDLED, ENV_EXTERNAL):
            raise ValueError(f"dbt env must be 'bundled' or 'external'; got {env!r}")
        self.env = env
        self.profiles_dir = Path(profiles_dir).resolve() if profiles_dir is not None else None
        self.timeout_seconds = timeout_seconds

    def run(self, command: DbtCommand) -> DbtRunResult:
        """Run ``command`` as a subprocess and normalize the on-disk artifacts."""
        argv = self._build_argv(command)
        target_dir = self.project_dir / "target"
        run_results_path = target_dir / "run_results.json"
        manifest_path = target_dir / "manifest.json"

        started = time.monotonic()
        completed = Subprocess.run_to_completion(
            argv,
            cwd=self.project_dir,
            timeout_seconds=self.timeout_seconds,
        )
        duration_ms = int((time.monotonic() - started) * 1000)

        report = read_run_results(
            run_results_path,
            manifest_path=manifest_path if manifest_path.is_file() else None,
        )
        return DbtRunResult.from_report(
            report,
            returncode=completed.returncode,
            logs=completed.output,
            duration_ms=duration_ms,
            manifest_ref=str(manifest_path) if manifest_path.is_file() else None,
            run_results_ref=str(run_results_path) if run_results_path.is_file() else None,
        )

    def _build_argv(self, command: DbtCommand) -> list[str]:
        """``[engine_bin, <command flags>, --project-dir …, [--profiles-dir …]]``."""
        argv = [self.dbt_executable, *command.to_argv()]
        argv += ["--project-dir", str(self.project_dir)]
        # `--profiles-dir` only for external dbt; the bundled engine resolves
        # its profiles from the managed env (connect's deferred half).
        if self.env == ENV_EXTERNAL and self.profiles_dir is not None:
            argv += ["--profiles-dir", str(self.profiles_dir)]
        return argv


class UnsupportedBackendError(NotImplementedError):
    """A config names a dbt backend that isn't implemented yet.

    Raised at *backend construction* (not config load): a config naming a
    deferred backend (``snowflake-native`` / ``dbt-cloud`` / ``remote``)
    round-trips and loads fine, but trying to *run* it surfaces a clear error
    here, where the deferred work lives.
    """


def build_backend(
    *,
    dbt_backend: str | None,
    dbt_executable: str | Path,
    project_dir: str | Path,
    dbt_env: str | None = None,
    profiles_dir: str | Path | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> LocalDbtBackend:
    """Construct the dbt backend a component names; only ``local`` is implemented.

    ``dbt_backend`` of ``None`` defaults to ``local``. A non-``local`` (but
    config-valid) backend raises :class:`UnsupportedBackendError` here — the
    "loads but not yet implemented" seam the config validator defers to.
    """
    backend = dbt_backend or "local"
    if backend != "local":
        raise UnsupportedBackendError(
            f"dbt backend {backend!r} is not yet implemented; only 'local' runs in this build."
        )
    return LocalDbtBackend(
        dbt_executable=dbt_executable,
        project_dir=project_dir,
        env=dbt_env or ENV_BUNDLED,
        profiles_dir=profiles_dir,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "ENV_BUNDLED",
    "ENV_EXTERNAL",
    "LocalDbtBackend",
    "UnsupportedBackendError",
    "build_backend",
]
