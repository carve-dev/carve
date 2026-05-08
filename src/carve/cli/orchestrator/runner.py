"""`carve el run` orchestration — target-aware artifact execution.

Reads an EL artifact by name from ``targets/<active>/el/<name>/``,
builds a `PythonStep` from the live files, runs it through
`LocalVenvRunner`, and tails the streaming logs to stdout. Returns a
process-style exit code so the typer command module can hand it to
`raise typer.Exit(code=...)`.

P1-07 retargeted path resolution: primary lookup is
``targets/<active>/el/<name>/main.py``. The legacy
``pipelines/<name>/main.py`` path from M1.1-06 is checked as a
transitional fallback with a one-line deprecation warning, then removed
in v0.2.

Two entry points:

* `run_pipeline_by_name(name, ...)` — primary API. Looks up the pipeline
  row and runs whatever's currently on disk. Re-runnable as often as
  needed; M1.1-06 removed the replay guard and P1-07 carries it forward.
* `run_pipeline_by_plan(plan_id, ...)` — debug/replay. Resolves to the
  pipeline the plan built and runs that.

The runner injects three ``CARVE_*`` env vars into the user subprocess
(via ``PythonStepConfig.env``):

* ``CARVE_ACTIVE_TARGET`` — uppercased, matches the ``<TARGET>_*``
  env-var prefix convention so the user script can do
  ``os.environ[f"{target}_SNOWFLAKE_USER"]``.
* ``CARVE_PIPELINE_NAME`` — the artifact name.
* ``CARVE_RUN_ID`` — the ``Run.id`` for this execution.

Exit code mapping:

* 0 — run finished successfully.
* 1 — run failed (non-zero exit, exception, missing files, etc.).
* 2 — usage error: pipeline not found, malformed plan id.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape as _escape

from carve.cli.orchestrator.planner import PLAN_ID_RE
from carve.core.config import Config
from carve.core.runners.local_venv import LocalVenvRunner
from carve.core.state import Repository
from carve.core.steps.base import RunContext
from carve.core.steps.python import PythonStep, PythonStepConfig

logger = logging.getLogger(__name__)


_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled", "crashed"})

# Polling interval for the live-tail loop. 250ms matches the runner's
# internal `stream_logs` cadence.
_TAIL_INTERVAL = 0.25


def run_pipeline_by_name(
    pipeline_name: str,
    config: Config,
    project_dir: Path,
    *,
    repository: Repository,
    console: Console | None = None,
    runner: LocalVenvRunner | None = None,
    target: str | None = None,
    auto_fix: bool | None = None,
    max_fix_attempts: int | None = None,
    recovery_client: Any | None = None,
) -> int:
    """Run the EL artifact named ``pipeline_name`` and return an exit code.

    ``target`` overrides the resolved active target (precedence:
    explicit ``target`` arg → ``CARVE_TARGET`` env → ``carve.toml``
    default_target → ``"dev"``). ``run_pipeline_by_name`` does *not*
    require a `Pipeline` row to exist — the on-disk artifact under
    ``targets/<active>/el/<name>/`` is the source of truth. A missing
    artifact maps to exit 2.
    """
    from carve.core.targets.resolution import (
        TargetResolutionError,
        resolve_active_target,
    )

    console = console or Console()
    project_dir = project_dir.resolve()

    try:
        active_target = resolve_active_target(target, config)
    except TargetResolutionError as exc:
        console.print(f"[red]✗[/red] {_escape(str(exc))}")
        return 2

    # Validate the target is actually defined in connections.toml so we
    # don't spawn a venv against a target whose creds aren't reachable.
    available = list(config.connections.snowflake.keys())
    if available and active_target not in available:
        listed = ", ".join(sorted(available))
        console.print(
            f'[red]✗[/red] target "{_escape(active_target)}" not defined in '
            f"carve/connections.toml.\n"
            f"  Available targets: {_escape(listed)}\n"
            f"  Create one with: carve target create {_escape(active_target)}"
        )
        return 2

    # Locate the artifact directory. Defense-in-depth: both candidate
    # paths are resolved and verified to live under ``project_dir`` so
    # a pathological pipeline name (e.g. ``..`` or absolute path) can't
    # escape the project root.
    primary_dir = (project_dir / "targets" / active_target / "el" / pipeline_name).resolve()
    legacy_dir = (project_dir / "pipelines" / pipeline_name).resolve()
    project_root = project_dir.resolve()
    for candidate in (primary_dir, legacy_dir):
        try:
            candidate.relative_to(project_root)
        except ValueError:
            console.print(
                f"[red]✗[/red] Pipeline directory escapes project root: "
                f"{_escape(pipeline_name)}"
            )
            return 1

    pipeline_dir_abs: Path | None = None
    if (primary_dir / "main.py").is_file():
        pipeline_dir_abs = primary_dir
    elif (legacy_dir / "main.py").is_file():
        console.print(
            f"[yellow]![/yellow] Found legacy 'pipelines/{_escape(pipeline_name)}/main.py' "
            f"at the project root. Migrate to "
            f"'targets/{_escape(active_target)}/el/{_escape(pipeline_name)}/' "
            f"(see CHANGELOG v0.1.0). Falling back for now."
        )
        logger.warning(
            "legacy 'pipelines/%s/main.py' fallback used; migrate to "
            "'targets/%s/el/%s/' (removed in v0.2).",
            pipeline_name,
            active_target,
            pipeline_name,
        )
        pipeline_dir_abs = legacy_dir

    if pipeline_dir_abs is None:
        console.print(
            f"[red]✗[/red] No EL artifact named "
            f"'{_escape(pipeline_name)}' in target "
            f"'{_escape(active_target)}'. "
            f"Run `carve el list --target {_escape(active_target)}` to see "
            f"what's available, or `carve build <plan_id>` to create it."
        )
        return 2

    # Most-recent successful Build for (pipeline, target). Used for
    # `target_id`. May be None for legacy/hand-built artifacts; we
    # fall back to the pipeline name in that case.
    latest_build = repository.latest_build_for(pipeline_name, active_target)
    # `target_id` semantics: the build id when one exists (so historical
    # filters can join run→build), else NULL-by-convention via the
    # pipeline name (legacy / hand-built artifacts).
    target_id = latest_build.id if latest_build is not None else pipeline_name

    # P1-09 wraps execution in `run_with_recovery` when auto-fix is
    # enabled. Default-None means "use the runner.toml setting"; an
    # explicit True / False overrides. The legacy default (no auto-fix)
    # keeps P1-07's tests stable; the el/run command's caller flips
    # the flag on per its own CLI args.
    auto_fix_resolved: bool
    if auto_fix is None:
        auto_fix_resolved = False
    else:
        auto_fix_resolved = auto_fix
    max_fix_attempts_resolved = (
        max_fix_attempts
        if max_fix_attempts is not None
        else config.runner.auto_fix.max_attempts
    )

    if auto_fix_resolved:
        return _run_pipeline_with_recovery(
            pipeline_name=pipeline_name,
            pipeline_dir_abs=pipeline_dir_abs,
            target_id=target_id,
            active_target=active_target,
            config=config,
            project_dir=project_dir,
            repository=repository,
            console=console,
            runner=runner,
            max_fix_attempts=max_fix_attempts_resolved,
            recovery_client=recovery_client,
        )

    return _run_pipeline_dir(
        pipeline_name=pipeline_name,
        pipeline_dir_abs=pipeline_dir_abs,
        target_id=target_id,
        active_target=active_target,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
        runner=runner,
    )


def run_pipeline_by_plan(
    plan_id: str,
    config: Config,
    project_dir: Path,
    *,
    repository: Repository,
    console: Console | None = None,
    runner: LocalVenvRunner | None = None,
) -> int:
    """Resolve ``plan_id`` to its pipeline and run that.

    Useful for debug-replay when a user wants to re-run "the version
    plan X built". Git is the safety net for drift checking — we only
    verify the pipeline points at a real on-disk script, not that the
    file contents match the original build's bytes.
    """
    console = console or Console()
    project_dir = project_dir.resolve()

    if not PLAN_ID_RE.match(plan_id):
        console.print(
            f"[red]✗[/red] Invalid plan id format: {_escape(plan_id)}"
        )
        return 2

    plan_row = repository.get_plan(plan_id)
    if plan_row is None:
        console.print(f"[red]✗[/red] Plan not found: {_escape(plan_id)}")
        return 2
    if plan_row.pipeline_name is None:
        console.print(
            f"[red]✗[/red] Plan {_escape(plan_id)} has not been built yet.\n"
            f"  Run `carve build {plan_id}` first."
        )
        return 2

    pipeline_row = repository.get_pipeline(plan_row.pipeline_name)
    if pipeline_row is None:
        console.print(
            f"[red]✗[/red] Plan {_escape(plan_id)} points at pipeline "
            f"{_escape(plan_row.pipeline_name)}, which is missing from the "
            "state store."
        )
        return 2

    return run_pipeline_by_name(
        pipeline_name=pipeline_row.name,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
        runner=runner,
    )


# ---------------------------------------------------------------------------
# Shared core
# ---------------------------------------------------------------------------


def _run_pipeline_dir(
    *,
    pipeline_name: str,
    pipeline_dir_abs: Path,
    target_id: str,
    active_target: str,
    config: Config,
    project_dir: Path,
    repository: Repository,
    console: Console,
    runner: LocalVenvRunner | None,
    parent_run_id: str | None = None,
) -> int:
    """Execute ``<pipeline_dir_abs>/main.py`` and tail logs.

    ``pipeline_dir_abs`` must already be an absolute, resolved path
    under ``project_dir``; the caller (`run_pipeline_by_name`) has
    already validated containment.
    """
    project_root = project_dir.resolve()
    # Defense-in-depth: refuse to run anything that resolves outside the
    # project root. Caller has already validated this for the standard
    # path-resolution path; this guard backstops hand-built call sites.
    try:
        pipeline_dir_abs.relative_to(project_root)
    except ValueError:
        console.print(
            f"[red]✗[/red] Pipeline directory escapes project root: "
            f"{_escape(str(pipeline_dir_abs))}"
        )
        return 1
    main_py = pipeline_dir_abs / "main.py"
    if not main_py.is_file():
        console.print(
            f"[red]✗[/red] Pipeline {_escape(pipeline_name)} is missing "
            f"`main.py` on disk: {_escape(str(main_py))}\n"
            "  Re-run `carve build` against a draft plan to regenerate."
        )
        return 1

    requirements_path = pipeline_dir_abs / "requirements.txt"
    requirements = _read_requirements(requirements_path)

    script_rel = main_py.relative_to(project_dir).as_posix()

    # Ensure a `Pipeline` row exists for FK integrity and so `carve
    # pipelines` / `carve runs --pipeline <name>` can index this run.
    # If the artifact was hand-built (no `carve build` has run for it),
    # this is the first time the row appears in the state store.
    if repository.get_pipeline(pipeline_name) is None:
        repository.create_or_update_pipeline(
            name=pipeline_name,
            description="",
            pipeline_dir=str(pipeline_dir_abs.relative_to(project_dir)),
        )

    # Reserve the run id ahead of `PythonStepConfig` construction so we
    # can stuff it into the env. The `Run` row is created right below.
    run_id = repository.create_run(
        kind="run",
        target_id=target_id,
        pipeline_name=pipeline_name,
        target=active_target,
        parent_run_id=parent_run_id,
    )
    # Stash so the recovery wrapper can pick it up after this call
    # returns. No-op when the wrapper isn't in play.
    _LAST_RUN_ID.set(run_id)

    try:
        step_config = PythonStepConfig(
            id=pipeline_name,
            script=script_rel,
            requirements=requirements,
            timeout_seconds=config.runner.default_timeout_seconds,
            env={
                # The agent-emitted user script reads
                # ``os.environ['CARVE_ACTIVE_TARGET']`` and uses it as
                # the prefix to look up its target-scoped credentials,
                # e.g. ``os.environ[f"{target}_SNOWFLAKE_USER"]``.
                "CARVE_ACTIVE_TARGET": active_target.upper(),
                "CARVE_PIPELINE_NAME": pipeline_name,
                "CARVE_RUN_ID": run_id,
            },
        )
    except ValueError as exc:
        console.print(f"[red]✗[/red] Pipeline config is malformed: {exc}")
        repository.update_run_status(run_id, "failed", error=str(exc))
        return 1

    step = PythonStep(step_config)

    runner = runner or LocalVenvRunner(config.runner, repository)

    context = RunContext(
        run_id=run_id,
        project_dir=project_dir,
        target=active_target,
        config=config,
    )

    console.print(f"[bold]Running pipeline[/bold] {_escape(pipeline_name)}")
    console.print(f"  Run id: {_escape(run_id)}")
    console.print(f"  Script: {_escape(script_rel)}")

    try:
        runner.execute(step, context)
    except ValueError as exc:
        console.print(f"[red]✗[/red] {exc}")
        repository.update_run_status(run_id, "failed", error=str(exc))
        repository.record_pipeline_run(
            pipeline_name=pipeline_name,
            run_id=run_id,
            status="failed",
        )
        return 1

    final_status = _tail_logs(repository, run_id, console)

    run = repository.get_run(run_id)
    duration_ms = run.duration_ms if run is not None else None
    error_message = run.error_message if run is not None else None
    completed_at = run.completed_at if run is not None else None

    repository.record_pipeline_run(
        pipeline_name=pipeline_name,
        run_id=run_id,
        status=final_status,
        run_at=completed_at,
    )

    if final_status == "success":
        console.print(
            f"[green]✓[/green] Run succeeded "
            f"({_format_duration(duration_ms)})"
        )
        return 0

    console.print(
        f"[red]✗[/red] Run {final_status} "
        f"({_format_duration(duration_ms)})"
    )
    if error_message:
        console.print(f"  {error_message}")
    return 1


# ---------------------------------------------------------------------------
# Recovery wrapper (P1-09)
# ---------------------------------------------------------------------------


def _run_pipeline_with_recovery(
    *,
    pipeline_name: str,
    pipeline_dir_abs: Path,
    target_id: str,
    active_target: str,
    config: Config,
    project_dir: Path,
    repository: Repository,
    console: Console,
    runner: LocalVenvRunner | None,
    max_fix_attempts: int,
    recovery_client: Any | None,
) -> int:
    """Run the pipeline, wrapping failures in `run_with_recovery`.

    Each retry creates a fresh Run row linked to the previous failed
    run via ``parent_run_id``. The loop is bounded by ``max_fix_attempts``;
    on exhaustion / refusal the original failure's exit code (1)
    surfaces along with the agent's diagnosis.
    """
    from carve.cli.orchestrator.recovery import (
        ExecutionResult,
        Exhausted,
        Recovered,
        Refused,
        run_with_recovery,
    )
    from carve.core.agents.recovery import ElRunInvocation

    invocation = ElRunInvocation(
        pipeline_name=pipeline_name,
        active_target=active_target,
        project_dir=project_dir,
        config=config,
        failed_run_id="",  # filled in by the orchestrator per attempt
        error_text="",
    )

    def _execute(parent_run_id: str | None) -> ExecutionResult:
        # Each attempt creates its own Run; pass `parent_run_id` through
        # so the chain is reachable via Run.parent_run_id.
        exit_code = _run_pipeline_dir(
            pipeline_name=pipeline_name,
            pipeline_dir_abs=pipeline_dir_abs,
            target_id=target_id,
            active_target=active_target,
            config=config,
            project_dir=project_dir,
            repository=repository,
            console=console,
            runner=runner,
            parent_run_id=parent_run_id,
        )
        run_id = _LAST_RUN_ID.get()
        run = repository.get_run(run_id) if run_id else None
        return ExecutionResult(
            run_id=run_id or "",
            success=exit_code == 0,
            error=(run.error_message or "") if run else "",
        )

    outcome = run_with_recovery(
        invocation,
        execute=_execute,
        repository=repository,
        max_attempts=max_fix_attempts,
        auto_fix=True,
        client=recovery_client,
    )

    if isinstance(outcome, Recovered):
        if outcome.attempts > 0:
            console.print(
                f"[green]✓[/green] Recovered after {outcome.attempts} attempt(s)"
            )
        return 0
    if isinstance(outcome, Exhausted):
        console.print(
            f"[red]✗[/red] Recovery exhausted after {outcome.attempts} attempt(s): "
            f"{_escape(outcome.diagnosis)}"
        )
        return 1
    if isinstance(outcome, Refused):
        console.print(
            f"[red]✗[/red] Failure category={outcome.category!r}: "
            f"{_escape(outcome.diagnosis)}"
        )
        return 1
    # Aborted
    console.print(
        f"[yellow]Aborted[/yellow] after {outcome.attempts} attempt(s)."
    )
    return 1


class _LastRunIdContext:
    """Module-local container so `_run_pipeline_dir` can hand the Run id back.

    `_run_pipeline_dir` returns an exit code per its existing P1-07
    contract. The recovery wrapper needs the run id too, but we can't
    change the return type without breaking tests. The simplest fix is
    a tiny per-call slot the wrapper reads after the call returns.
    Thread-local is overkill — the recovery loop is single-threaded.
    """

    _value: str | None = None

    def set(self, run_id: str) -> None:
        self._value = run_id

    def get(self) -> str | None:
        v = self._value
        self._value = None
        return v


_LAST_RUN_ID = _LastRunIdContext()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_requirements(path: Path) -> list[str]:
    """Read a ``requirements.txt`` from disk, filtering blanks/comments/flags.

    Pipeline files may have been hand-edited since the last build. We
    pick up whatever is on disk now rather than caching the build's
    snapshot — that's the whole point of `carve run` decoupling from
    plans.
    """
    if not path.is_file():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            logger.warning(
                "skipping flag-shaped requirement %r from %s; M1 only "
                "accepts plain package specs.",
                line,
                path,
            )
            continue
        out.append(line)
    return out


def _tail_logs(
    repository: Repository,
    run_id: str,
    console: Console,
) -> str:
    """Poll the repository, print new log lines, return the final status."""
    last_seen_id: int | None = None
    while True:
        logs = repository.get_logs(run_id, since_id=last_seen_id)
        for log in logs:
            console.print(_format_log_line(log.timestamp, log.level, log.source, log.message))
        if logs:
            last_seen_id = max(log.id for log in logs)

        run = repository.get_run(run_id)
        if run is None:
            return "crashed"
        if run.status in _TERMINAL_STATUSES and not logs:
            return run.status

        time.sleep(_TAIL_INTERVAL)


def _format_log_line(timestamp: datetime, level: str, source: str, message: str) -> str:
    ts = timestamp.replace(tzinfo=UTC).strftime("%H:%M:%S") if timestamp else ""
    color = {
        "info": "white",
        "warning": "yellow",
        "error": "red",
        "debug": "dim",
    }.get(level, "white")
    return f"[dim]{ts}[/dim] [{color}]{level:>7}[/{color}] [cyan]{source}[/cyan]  {message}"


def _format_duration(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "?"
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    return f"{duration_ms / 1000:.1f}s"


__all__ = ["run_pipeline_by_name", "run_pipeline_by_plan"]
