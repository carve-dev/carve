"""`carve apply` orchestration.

Reads a previously-saved plan, builds a `PythonStep`, runs it through
`LocalVenvRunner`, and tails the streaming logs to stdout. Returns a
process-style exit code so the typer command module can hand it to
``raise typer.Exit(code=...)``.

Exit code mapping:

* 0 — run finished successfully
* 1 — plan not found, plan already applied, or run failed (non-zero
  exit, exception, etc.)
* 2 — plan_id failed format validation (config/usage error)

In M1 there is no dedicated connection-error exit code; a Snowflake
connection failure inside the user script manifests as a non-zero
subprocess exit (and therefore exit 1). M2's plan/apply will surface
guardrail violations (exit code 4).
"""

from __future__ import annotations

import json
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
# internal `stream_logs` cadence; sync polling avoids dragging asyncio
# into the CLI's main path.
_TAIL_INTERVAL = 0.25


def apply_plan(
    plan_id: str,
    config: Config,
    project_dir: Path,
    *,
    repository: Repository,
    console: Console | None = None,
    runner: LocalVenvRunner | None = None,
) -> int:
    """Apply `plan_id` and return a CLI exit code."""
    console = console or Console()
    project_dir = project_dir.resolve()

    # Defence-in-depth format validation. The planner only ever emits
    # ids matching this regex, so anything else is either a typo or a
    # crafted input. Reject before any DB lookup or rich rendering so
    # neither path sees an unvetted string.
    if not PLAN_ID_RE.match(plan_id):
        console.print(
            f"[red]✗[/red] Invalid plan id format: {_escape(plan_id)}"
        )
        return 2

    plan_row = repository.get_plan(plan_id)
    if plan_row is None:
        console.print(f"[red]✗[/red] Plan not found: {_escape(plan_id)}")
        return 1

    if plan_row.applied_at is not None:
        applied_ts = plan_row.applied_at.replace(tzinfo=UTC).isoformat()
        prior_run = plan_row.apply_run_id or "<unknown>"
        console.print(
            f"[red]✗[/red] Plan {_escape(plan_id)} was already applied at "
            f"{applied_ts} (run_id={_escape(prior_run)}). Re-running plans "
            "is not supported in M1; generate a new plan."
        )
        return 1

    plan_doc = _load_plan_json(plan_row.file_path, console)
    if plan_doc is None:
        return 1

    if plan_row.config_hash != config.config_hash:
        console.print(
            "[yellow]![/yellow] Config has changed since this plan was generated; "
            "applying anyway. Re-plan for an up-to-date estimate."
        )

    try:
        step_config = PythonStepConfig(
            id=plan_id,
            script=str(plan_doc["script_path"]),
            requirements=list(plan_doc.get("requirements", []) or []),
            timeout_seconds=config.runner.default_timeout_seconds,
            env={},
        )
    except (KeyError, ValueError) as exc:
        console.print(f"[red]✗[/red] Plan is malformed: {exc}")
        return 1

    step = PythonStep(step_config)

    runner = runner or LocalVenvRunner(config.runner, repository)

    run_id = repository.create_run(kind="apply", target_id=plan_id)
    repository.mark_plan_applied(plan_id, run_id)

    context = RunContext(
        run_id=run_id,
        project_dir=project_dir,
        target=config.project.default_target,
        config=config,
    )

    console.print(f"[bold]Applying plan[/bold] {_escape(plan_id)}")
    console.print(f"  Run id: {_escape(run_id)}")
    console.print(f"  Script: {_escape(str(plan_doc['script_path']))}")

    try:
        runner.execute(step, context)
    except ValueError as exc:
        console.print(f"[red]✗[/red] {exc}")
        repository.update_run_status(run_id, "failed", error=str(exc))
        return 1

    final_status = _tail_logs(repository, run_id, console)

    run = repository.get_run(run_id)
    duration_ms = run.duration_ms if run is not None else None
    error_message = run.error_message if run is not None else None

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


def _load_plan_json(file_path: str, console: Console) -> dict[str, Any] | None:
    """Load the plan JSON from disk, or surface an error and return None."""
    path = Path(file_path)
    if not path.is_file():
        console.print(
            f"[red]✗[/red] Plan file is missing on disk: {file_path}\n"
            "  Re-run `carve plan` to regenerate it."
        )
        return None
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]✗[/red] Failed to read plan JSON: {exc}")
        return None


def _tail_logs(
    repository: Repository,
    run_id: str,
    console: Console,
) -> str:
    """Poll the repository, print new log lines, return the final status.

    Stops once the run reaches a terminal state and there are no more
    pending log lines to print. Uses `Log.id` (autoincrement) as the
    cursor: filtering by timestamp drops lines that share a tick with
    the previous batch, which can swallow the trailing log of a run
    that completes within a single millisecond.
    """
    last_seen_id: int | None = None
    while True:
        logs = repository.get_logs(run_id, since_id=last_seen_id)
        for log in logs:
            console.print(_format_log_line(log.timestamp, log.level, log.source, log.message))
        if logs:
            last_seen_id = max(log.id for log in logs)

        run = repository.get_run(run_id)
        if run is None:
            # Defensive — the row should exist; if it disappeared we
            # treat that as a failed run rather than spinning forever.
            return "crashed"
        if run.status in _TERMINAL_STATUSES and not logs:
            return run.status

        time.sleep(_TAIL_INTERVAL)


def _format_log_line(timestamp: datetime, level: str, source: str, message: str) -> str:
    """Format a log line for the CLI tail.

    Rich-friendly: log level is colour-coded so warnings/errors stand out.
    """
    ts = timestamp.replace(tzinfo=UTC).strftime("%H:%M:%S") if timestamp else ""
    color = {
        "info": "white",
        "warning": "yellow",
        "error": "red",
        "debug": "dim",
    }.get(level, "white")
    return f"[dim]{ts}[/dim] [{color}]{level:>7}[/{color}] [cyan]{source}[/cyan]  {message}"


def _format_duration(duration_ms: int | None) -> str:
    """Render a duration as a short human string (`1.2s`, `45ms`, etc.)."""
    if duration_ms is None:
        return "?"
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    return f"{duration_ms / 1000:.1f}s"


__all__ = ["apply_plan"]
