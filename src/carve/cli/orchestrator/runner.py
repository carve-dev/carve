"""`carve run` orchestration — pipeline-name-keyed execution.

Reads a pipeline by name, builds a `PythonStep` from the live files
under ``pipelines/<name>/``, runs it through `LocalVenvRunner`, and
tails the streaming logs to stdout. Returns a process-style exit code
so the typer command module can hand it to `raise typer.Exit(code=...)`.

Two entry points:

* `run_pipeline_by_name(name, ...)` — primary API. Looks up the pipeline
  row and runs whatever's currently on disk. Re-runnable as often as
  needed; M1.1-06 removed the replay guard from this path.
* `run_pipeline_by_plan(plan_id, ...)` — debug/replay. Resolves to the
  pipeline the plan built and runs that.

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
) -> int:
    """Run the pipeline named ``pipeline_name`` and return an exit code."""
    console = console or Console()
    project_dir = project_dir.resolve()

    pipeline_row = repository.get_pipeline(pipeline_name)
    if pipeline_row is None:
        console.print(
            f"[red]✗[/red] Pipeline not found: {_escape(pipeline_name)}\n"
            "  Use `carve pipelines` to see what's available, or "
            "`carve plan \"<goal>\"` then `carve build <plan_id>` to create one."
        )
        return 2

    return _run_pipeline_dir(
        pipeline_name=pipeline_row.name,
        pipeline_dir_rel=pipeline_row.pipeline_dir,
        plan_id=pipeline_row.current_plan_id,
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

    return _run_pipeline_dir(
        pipeline_name=pipeline_row.name,
        pipeline_dir_rel=pipeline_row.pipeline_dir,
        plan_id=plan_row.id,
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
    pipeline_dir_rel: str,
    plan_id: str | None,
    config: Config,
    project_dir: Path,
    repository: Repository,
    console: Console,
    runner: LocalVenvRunner | None,
) -> int:
    """Execute ``<project_dir>/<pipeline_dir_rel>/main.py`` and tail logs."""
    pipeline_dir_abs = (project_dir / pipeline_dir_rel).resolve()
    # Defense-in-depth: refuse to run anything that resolves outside the
    # project root. The migration backfill or a hand-edited state DB
    # could in theory land an absolute path or a `..` traversal in
    # `pipeline_dir`; mirroring `_resolve_under_root` from m1_tools.py
    # keeps the runner consistent with the agent's path guard.
    project_root = project_dir.resolve()
    try:
        pipeline_dir_abs.relative_to(project_root)
    except ValueError:
        console.print(
            f"[red]✗[/red] Pipeline directory escapes project root: "
            f"{_escape(pipeline_dir_rel)}"
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

    try:
        step_config = PythonStepConfig(
            id=pipeline_name,
            script=script_rel,
            requirements=requirements,
            timeout_seconds=config.runner.default_timeout_seconds,
            env={},
        )
    except ValueError as exc:
        console.print(f"[red]✗[/red] Pipeline config is malformed: {exc}")
        return 1

    step = PythonStep(step_config)

    runner = runner or LocalVenvRunner(config.runner, repository)

    target_id = plan_id or pipeline_name
    run_id = repository.create_run(
        kind="run",
        target_id=target_id,
        pipeline_name=pipeline_name,
    )

    context = RunContext(
        run_id=run_id,
        project_dir=project_dir,
        target=config.project.default_target,
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
