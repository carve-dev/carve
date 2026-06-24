"""The ``dlt`` step executor — run a dlt component, parse its load package.

``DltStepExecutor`` implements the Unit-1 :class:`StepExecutor` seam for a
``dlt`` step. It resolves the step's ``component`` name to a code dir, runs
the component's Python entrypoint via the **injected** run mechanism (default:
the shipped venv/subprocess primitive), then reads the on-disk **load package**
to decide the verdict and shape the step ``outputs``.

Why run the component's module (not a CLI)
------------------------------------------
``dlt`` 1.28 has **no** ``dlt pipeline run`` CLI (see
:func:`carve.integrations.dlt.runner.dlt_inspect_command`). A dlt component *is*
its Python module, executed in its own venv. So this executor runs
``<venv-python> <entrypoint>`` via :class:`~carve.core.runners.subprocess.Subprocess`
(own process group, Carve secrets stripped, wall-clock watchdog), exactly the
discipline the dlt-engineer verify path established — then
:func:`~carve.integrations.dlt.verify.parse_dlt_run` /
:func:`~carve.integrations.dlt.verify.read_latest_load_package` reads the load
package the run wrote. The verdict comes from the package (``loaded`` state +
no ``failed_jobs``), exit-code-gated — never the exit code alone.

The run mechanism is injected (:data:`DltRunFn`) so the offline test layer
never spawns a real venv; one ``importorskip("dlt")``-gated test runs a real
DuckDB load and parses its package end to end.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from carve.integrations.component_locator import ComponentResolutionError
from carve.integrations.dlt.verify import LoadPackageReport, read_latest_load_package
from carve.runtime.component_resolution import resolve_dlt_component
from carve.runtime.step_executor import StepResult

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.pipeline_schema import DltStepConfig, PipelineStep
    from carve.core.config.schema import ComponentConfig
    from carve.runtime.run_context import PipelineRun

# Default per-step wall-clock budget for a dlt load (ARCHITECTURE §14.6: 4h).
DEFAULT_DLT_TIMEOUT_SECONDS = 14400

# The conventional component entrypoint, relative to the resolved code dir. A
# Carve dlt component runs its module's ``run()`` via ``python <entrypoint>``
# (the reference HN pack ships ``scripts/__init__.py`` with a ``__main__``
# guard); the candidates are tried in order so a flat ``pipeline.py`` works too.
_ENTRYPOINT_CANDIDATES: tuple[str, ...] = (
    "scripts/__init__.py",
    "pipeline.py",
    "__init__.py",
    "main.py",
)


@dataclass(frozen=True)
class DltRunOutcome:
    """The result of running a dlt component's entrypoint to completion.

    ``returncode`` is the process exit code; ``output`` is the combined
    stdout/stderr tail used for the error summary; ``duration_ms`` is the
    wall-clock run time. ``timed_out`` flags a watchdog kill.
    """

    returncode: int
    output: str
    duration_ms: int
    timed_out: bool = False


# The injected run mechanism: given the entrypoint + cwd + env + timeout, run
# the component to completion and return its outcome. Defaults to the shipped
# venv/subprocess primitive (:func:`_default_run_component`); tests inject a
# fake so they never spawn a real venv.
DltRunFn = Callable[..., DltRunOutcome]


class DltStepExecutor:
    """Run a ``dlt`` step: resolve the component, run it, parse the package."""

    step_type = "dlt"

    def __init__(
        self,
        *,
        components: dict[str, ComponentConfig] | None = None,
        run_fn: DltRunFn | None = None,
        timeout_seconds: int = DEFAULT_DLT_TIMEOUT_SECONDS,
    ) -> None:
        """Build the executor.

        Args:
            components: ``[components.*]`` blocks for name resolution
                (defaults to empty == simple mode).
            run_fn: The injected run mechanism (defaults to the shipped
                venv/subprocess primitive). Tests inject a fake so no real
                venv is spawned.
            timeout_seconds: Per-step wall-clock budget for the load.
        """
        self._components = components or {}
        self._run_fn = run_fn or _default_run_component
        self._timeout_seconds = timeout_seconds

    async def execute(
        self,
        *,
        step: PipelineStep,
        run: PipelineRun,
        paths: ProjectPaths,
    ) -> StepResult:
        """Resolve, run, and parse one ``dlt`` step into a :class:`StepResult`."""
        dlt_step = _as_dlt_step(step)

        # FIX-D1: per-step write_disposition / resource_select overrides are not
        # yet honored by the run mechanism — the keys Carve invented were never
        # read by dlt, so forwarding them would silently ignore the override and
        # still report success. Fail loud instead of false-green.
        if dlt_step.write_disposition is not None or dlt_step.resource_select:
            return StepResult(
                status="failed",
                error_message=(
                    "per-step dlt write_disposition/resource_select override is not yet "
                    "supported by the dlt step executor; set it in the component "
                    "(deferred to live dlt wiring)."
                ),
            )

        try:
            resolved = resolve_dlt_component(dlt_step.component, paths, components=self._components)
        except ComponentResolutionError as exc:
            return StepResult(
                status="failed",
                error_message=f"dlt component {dlt_step.component!r} did not resolve: {exc}",
            )

        code_dir = resolved.code_path
        if not code_dir.exists():
            return StepResult(
                status="failed",
                error_message=f"dlt component dir not found: {code_dir}",
            )

        entrypoint = _find_entrypoint(code_dir)
        if entrypoint is None:
            return StepResult(
                status="failed",
                error_message=(
                    f"dlt component {dlt_step.component!r} has no runnable entrypoint "
                    f"(looked for {', '.join(_ENTRYPOINT_CANDIDATES)} under {code_dir})."
                ),
            )

        # Pin dlt's data dir to a known location so the load package lands
        # where we can read it — a dlt component's *internal* pipeline_name is
        # script-chosen and need not match the component name, so we discover
        # the written pipeline subdir by scanning this dir after the run.
        pipelines_dir = paths.dlt_config_dir / "pipelines"
        env = _build_dlt_env(paths)

        # The recency cutoff for THIS run: dlt's load_id is ``str(unix_time)``,
        # so a package this run wrote has ``load_id >= run.started_at`` (in
        # epoch seconds). Packages older than this are stale leftovers from a
        # prior run — trusting one would report ``succeeded`` off tables this run
        # never touched (FIX-D2 residual). Threaded into discovery + verdict so
        # neither path trusts a stale package.
        min_load_id = run.started_at.timestamp()

        # Offload the blocking run to a thread — the DAG walk is async.
        outcome = await asyncio.to_thread(
            self._run_fn,
            entrypoint=entrypoint,
            code_dir=code_dir,
            cwd=paths.root,
            env=env,
            timeout_seconds=self._timeout_seconds,
        )

        return _outcome_to_step_result(
            outcome,
            pipelines_dir=pipelines_dir,
            fallback_name=resolved.name,
            min_load_id=min_load_id,
        )


def _as_dlt_step(step: PipelineStep) -> DltStepConfig:
    """Narrow ``step`` to a ``dlt`` step (the registry guarantees the type)."""
    from carve.core.config.pipeline_schema import DltStepConfig

    if not isinstance(step, DltStepConfig):
        raise TypeError(f"DltStepExecutor received a {step.type!r} step: {step.id!r}")
    return step


def _find_entrypoint(code_dir: Path) -> Path | None:
    """Find the component's runnable entrypoint under ``code_dir``."""
    for candidate in _ENTRYPOINT_CANDIDATES:
        path = code_dir / candidate
        if path.is_file():
            return path
    return None


def _build_dlt_env(paths: ProjectPaths) -> dict[str, str]:
    """Build the dlt env overrides for the component invocation.

    Pins ``DLT_DATA_DIR`` so the load package lands where the executor reads
    it. Per-step ``write_disposition`` / ``resource_select`` overrides are
    **not** layered here: the keys Carve previously forwarded
    (``CARVE_DLT_*``) were never read by dlt, so they silently no-op'd — those
    overrides now fail loud at the top of :meth:`DltStepExecutor.execute` until
    live dlt wiring honors them (FIX-D1).

    Destination credentials are **not** injected here today: the default
    :func:`_default_run_component` uses :class:`Subprocess` directly (not
    ``LocalVenvRunner``), so no ``_snowflake_env`` layering happens yet —
    deferred to live wiring (FIX-D3). The creds-free DuckDB substrate needs
    none, so the offline path is unaffected.
    """
    return {"DLT_DATA_DIR": str(paths.dlt_config_dir)}


def _outcome_to_step_result(
    outcome: DltRunOutcome,
    *,
    pipelines_dir: Path,
    fallback_name: str,
    min_load_id: float,
) -> StepResult:
    """Map a finished dlt run + its load package to a :class:`StepResult`.

    Verdict mirrors :func:`carve.integrations.dlt.verify.parse_dlt_run`: a
    non-zero exit is a failure; on a clean exit the on-disk package is the
    source of truth (``loaded`` state + no ``failed_jobs``). ``outputs`` carries
    the load-package fields (``tables``/``schema_changes``/``failed_jobs``);
    per-table row counts are not in the persisted package, so they're omitted.

    The dlt *pipeline name* under ``pipelines_dir`` is script-chosen and need
    not match the component name, so the subdir holding the **newest** load
    package **from this run** is discovered (:func:`_discover_pipeline_name`)
    rather than assumed to be the component name.

    FIX-D2 — false-green guard: if ``pipelines_dir`` exists but **no** subdir
    holds a load package, the run produced nothing to verify → ``failed``
    ("produced no load package"), never succeeded-with-empty. ``pipelines_dir``
    absent entirely is the distinct "nothing pinned here, trust the exit code"
    case (a component that writes its package elsewhere).

    FIX-D2 residual — cross-run staleness: ``DLT_DATA_DIR`` is **persistent**
    (nothing wipes it per run), so a re-run that exits 0 but writes no new
    package would otherwise read a PRIOR run's ``loaded`` package and report
    ``succeeded`` off stale tables. ``min_load_id`` (``run.started_at`` in epoch
    seconds) is the cutoff: discovery + verdict ignore any package whose
    ``load_id`` predates this run. A data dir that exists with only stale
    packages → ``failed``, never succeeded-with-stale.
    """
    log_lines = outcome.output.splitlines()
    if outcome.returncode != 0:
        reason = "timed out" if outcome.timed_out else f"exited {outcome.returncode}"
        return StepResult(
            status="failed",
            error_message=_error_summary(outcome.output) or f"dlt run {reason}.",
            log_lines=log_lines,
            duration_ms=outcome.duration_ms,
        )

    pipeline_name = _discover_pipeline_name(pipelines_dir, fallback_name, min_load_id)
    if pipeline_name is None:
        if not pipelines_dir.is_dir():
            # No data dir pinned here at all — nothing to introspect, trust the
            # exit code (a component that wrote its package outside DLT_DATA_DIR).
            return StepResult(
                status="succeeded",
                outputs={"tables": [], "schema_changes": [], "failed_jobs": []},
                log_lines=log_lines,
                duration_ms=outcome.duration_ms,
            )
        # The data dir exists but no subdir holds a load package FROM THIS RUN:
        # the run loaded nothing new we can verify (only stale packages from
        # prior runs, if any). Fail loud — never false-green off a stale package.
        return StepResult(
            status="failed",
            error_message=(
                "dlt run produced no new load package this run "
                "(only stale packages from prior runs)."
            ),
            log_lines=log_lines,
            duration_ms=outcome.duration_ms,
        )

    report = read_latest_load_package(pipelines_dir, pipeline_name)
    if report is None or _as_float(report.load_id) < min_load_id:
        # Discovery found a load-bearing subdir but its newest package didn't
        # parse, or is stale (from a prior run — its load_id predates this run's
        # start). Either way there is no verifiable package FROM THIS RUN — fail
        # loud rather than trust a stale or unparseable one.
        return StepResult(
            status="failed",
            error_message=(
                "dlt run produced no new load package this run "
                "(only stale packages from prior runs)."
            ),
            log_lines=log_lines,
            duration_ms=outcome.duration_ms,
        )

    outputs = _report_outputs(report)
    passed = report.completed and not report.failed_jobs
    if passed:
        return StepResult(
            status="succeeded",
            outputs=outputs,
            log_lines=log_lines,
            duration_ms=outcome.duration_ms,
        )

    reason = (
        f"{len(report.failed_jobs)} failed job(s)"
        if report.failed_jobs
        else f"load package {report.load_id} did not reach 'loaded'"
    )
    return StepResult(
        status="failed",
        outputs=outputs,
        error_message=f"dlt load did not complete cleanly: {reason}.",
        log_lines=log_lines,
        duration_ms=outcome.duration_ms,
    )


def _discover_pipeline_name(
    pipelines_dir: Path, fallback_name: str, min_load_id: float
) -> str | None:
    """Find the pipeline subdir holding a parseable package FROM THIS RUN.

    dlt writes ``<pipelines_dir>/<pipeline_name>/load/`` where ``pipeline_name``
    is the value the component's script passed to ``dlt.pipeline(...)`` — not
    necessarily the component name (the reference HN pack's name is
    ``hacker_news`` regardless of its ``el/<dir>``). Resolution:

    1. Prefer the fallback (component name) when its newest package is from this
       run (``load_id >= min_load_id``).
    2. Otherwise pick the subdir whose **newest in-window** package has the
       highest ``load_id`` — reusing :func:`read_latest_load_package`'s ordering
       — so a component whose pipeline_name differs from its dir still resolves
       (FIX-D2: no silent fallback to a load-less component name).
    3. Return ``None`` when no subdir holds a parseable package from this run, so
       the caller fails loud instead of reporting succeeded-with-empty or
       succeeded-off-a-stale-package.

    ``min_load_id`` (``run.started_at`` in epoch seconds) filters out stale
    packages from prior runs: ``DLT_DATA_DIR`` is persistent, so a re-run that
    wrote nothing new must NOT resolve a prior run's package (FIX-D2 residual).
    A package whose ``load_id`` predates this run is ignored everywhere.
    """
    fallback = read_latest_load_package(pipelines_dir, fallback_name)
    if fallback is not None and _as_float(fallback.load_id) >= min_load_id:
        return fallback_name
    if not pipelines_dir.is_dir():
        return None

    # Rank every load-bearing subdir by its newest package's load_id; the
    # highest wins (the most recent run's package). A string tie-break keeps a
    # non-numeric load_id (shouldn't happen) sorting safely, matching
    # read_latest_load_package's own ordering. Packages older than this run's
    # start are stale leftovers and skipped — never trusted.
    best_name: str | None = None
    best_key: tuple[float, str] | None = None
    for child in sorted(pipelines_dir.iterdir()):
        if not child.is_dir():
            continue
        report = read_latest_load_package(pipelines_dir, child.name)
        if report is None:
            continue
        load_id_f = _as_float(report.load_id)
        if load_id_f < min_load_id:
            continue  # stale package from a prior run — ignore.
        key = (load_id_f, report.load_id)
        if best_key is None or key > best_key:
            best_name, best_key = child.name, key
    return best_name


def _as_float(load_id: str) -> float:
    """Parse a dlt load id (``str(unix_time)``) for newest-wins ordering."""
    try:
        return float(load_id)
    except ValueError:
        return float("-inf")


def _report_outputs(report: LoadPackageReport) -> dict[str, list[str]]:
    """JSON-serializable ``outputs`` dict from a load-package report."""
    return {
        "tables": list(report.tables),
        "schema_changes": list(report.schema_changes),
        "failed_jobs": list(report.failed_jobs),
    }


_ERROR_MARKERS = ("Error", "Exception", "Failed", "Traceback")


def _error_summary(output: str) -> str:
    """The most informative error line (last marker line, else the last line)."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if any(marker in line for marker in _ERROR_MARKERS):
            return line[:300]
    return lines[-1][:300] if lines else ""


def _default_run_component(
    *,
    entrypoint: Path,
    code_dir: Path,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> DltRunOutcome:
    """Run a dlt component via the shipped subprocess primitive.

    Runs ``<python> <entrypoint>`` via
    :class:`~carve.core.runners.subprocess.Subprocess` — own process group,
    Carve secrets stripped, wall-clock watchdog — against the **ambient**
    interpreter today (the creds-free DuckDB substrate runs against the
    installed ``dlt``).

    Deferred to live wiring (connect) — honest about what does NOT happen yet:

    * **No venv materialization.** ``_resolve_component_python`` returns ``None``
      unconditionally, so a component's ``requirements.txt`` is *not* honored —
      the pinned deps are ignored until the runner materializes a venv here.
    * **No destination-cred injection.** This uses ``Subprocess`` directly, not
      ``LocalVenvRunner``, so the runner's ``_snowflake_env`` layering does not
      run — only the ``DLT_DATA_DIR`` env from ``_build_dlt_env`` reaches the
      child. Fine for DuckDB (no creds); a real warehouse needs the live wiring.

    DEFERRED (reconcile with the dlt-engineer): the component-run **entrypoint
    convention**. The candidate order is
    ``scripts/__init__.py`` → ``pipeline.py`` → ``__init__.py`` → ``main.py``,
    which works for the reference HN pack (its ``scripts/__init__.py`` has a
    ``__main__`` guard that calls ``run()``). But a carve-authored
    ``el/<name>/__init__.py`` written as a *source module* with no ``__main__``
    guard would be ``python __init__.py``-imported and exit **without running a
    load** — a false success. The bare-source convention and venv pooling
    (``SnowflakePool``) are live-wiring/runtime concerns.
    """
    import sys

    from carve.core.runners.subprocess import Subprocess

    python = _resolve_component_python(code_dir)
    started = time.monotonic()
    completed = Subprocess.run_to_completion(
        [python or sys.executable, str(entrypoint)],
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        extra_env=env,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    return DltRunOutcome(
        returncode=completed.returncode,
        output=completed.output,
        duration_ms=duration_ms,
        timed_out=completed.timed_out,
    )


def _resolve_component_python(code_dir: Path) -> str | None:
    """Always ``None`` today — "use the ambient interpreter".

    A component's ``requirements.txt`` is **not** honored yet: materializing a
    venv from it is the runner's job, deferred to live wiring (connect). Both
    branches previously returned ``None`` (a dead ``if``), so this is collapsed
    to a single honest ``return None`` — the pinned requirements are silently
    ignored until the live wiring drops the venv python in here. Kept as a seam
    so that wiring needs no executor change. ``code_dir`` is retained in the
    signature for that future use.
    """
    return None


__all__ = [
    "DEFAULT_DLT_TIMEOUT_SECONDS",
    "DltRunFn",
    "DltRunOutcome",
    "DltStepExecutor",
]
