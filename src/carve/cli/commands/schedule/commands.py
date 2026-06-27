"""``carve schedule list/show/pause/resume/set-cron`` — the live schedule surface.

These operate on the ``schedules`` table as **data**: ``pause``/``resume``/
``set-cron`` mutate the live row instantly (the change takes effect within one
``carve serve`` loop interval, no deploy/reconcile) and append a
``schedule_changes`` audit row; ``list``/``show`` read it.

Each command follows ``carve worker``'s setup block — ``load_config`` → resolve
the active target → build the engine → ``initialize_database`` → session factory
→ construct the :class:`Schedules` repo. The cron expression (``set-cron``) and
the timezone (``--timezone``) are validated **up front** (croniter / zoneinfo)
and a bad value exits 2 before any DB write. ``actor_token_id`` is ``None`` and
``source='cli'`` this slice — the auth slice fills the token later.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from carve.core.config import ConfigError, load_config
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.schedules import ScheduleNotFound, Schedules
from carve.core.targets.resolution import resolve_active_target
from carve.runtime.cron import CronError, is_valid_cron, is_valid_timezone

console = Console()


def _build_schedules() -> tuple[Schedules, object]:
    """Run the shared setup block; return the repo + the engine (to dispose).

    Mirrors ``carve worker``: ``load_config`` → resolve the active target (so the
    state-store URL is read from the right place) → engine → migrate → session
    factory → :class:`Schedules`. Exits 2 with a rendered ``ConfigError`` on a
    bad/absent config.
    """
    project_dir = Path.cwd()
    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    # Read the resolved ``--target`` flag at call time (mutated by the main
    # callback after import — a module-level import would bind ``None``).
    from carve.cli.main import ACTIVE_TARGET_FLAG

    resolve_active_target(ACTIVE_TARGET_FLAG, config)

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    schedules = Schedules(create_session_factory(engine))
    return schedules, engine


def _fmt(value: datetime | None) -> str:
    """Render a UTC-aware datetime compactly, or ``-`` for NULL."""
    return value.strftime("%Y-%m-%d %H:%M:%SZ") if value is not None else "-"


def list_command() -> None:
    """List every schedule (cron, target, paused gate, next fire time)."""
    schedules, engine = _build_schedules()
    try:
        rows = schedules.list_all()
    finally:
        engine.dispose()  # type: ignore[attr-defined]

    if not rows:
        console.print("[yellow]No schedules.[/yellow]")
        raise typer.Exit(code=0)

    table = Table(title="Schedules")
    table.add_column("Pipeline")
    table.add_column("Cron")
    table.add_column("Timezone")
    table.add_column("Target")
    table.add_column("Paused")
    table.add_column("Next fire (UTC)")
    for sched in rows:
        paused = f"[red]yes ({sched.paused_by})[/red]" if sched.paused else "[green]no[/green]"
        table.add_row(
            escape(sched.pipeline),
            escape(sched.cron),
            escape(sched.timezone),
            escape(sched.target),
            paused,
            _fmt(sched.next_fires_at),
        )
    console.print(table)
    raise typer.Exit(code=0)


def show_command(
    pipeline: str = typer.Argument(..., help="Pipeline whose schedule to show."),
) -> None:
    """Show one schedule + its recent change history."""
    schedules, engine = _build_schedules()
    try:
        sched = schedules.get(pipeline)
        changes = schedules.list_changes(pipeline) if sched is not None else []
    finally:
        engine.dispose()  # type: ignore[attr-defined]

    if sched is None:
        console.print(f"[red]No schedule for pipeline {escape(pipeline)!r}.[/red]")
        raise typer.Exit(code=1)

    console.print(f"[bold]{escape(sched.pipeline)}[/bold]")
    console.print(f"  cron:          {escape(sched.cron)}  ({escape(sched.timezone)})")
    console.print(f"  target:        {escape(sched.target)}")
    paused = (
        f"[red]yes (by {sched.paused_by}: {escape(sched.pause_reason or '')})[/red]"
        if sched.paused
        else "[green]no[/green]"
    )
    console.print(f"  paused:        {paused}")
    console.print(f"  last fired:    {_fmt(sched.last_fired_at)}")
    console.print(f"  next fires:    {_fmt(sched.next_fires_at)}")

    if changes:
        table = Table(title="Recent changes")
        table.add_column("When (UTC)")
        table.add_column("Kind")
        table.add_column("Source")
        table.add_column("Reason")
        for change in changes[:10]:
            table.add_row(
                _fmt(change.changed_at),
                escape(change.change_kind),
                escape(change.source),
                escape(change.reason or ""),
            )
        console.print(table)
    raise typer.Exit(code=0)


def pause_command(
    pipeline: str = typer.Argument(..., help="Pipeline to pause."),
    reason: str | None = typer.Option(None, "--reason", help="Why the schedule is paused."),
) -> None:
    """Pause a schedule (stops firing within one loop interval; audited)."""
    schedules, engine = _build_schedules()
    try:
        schedules.pause(pipeline, reason=reason, source="cli", actor_token_id=None)
    except ScheduleNotFound:
        console.print(f"[red]No schedule for pipeline {escape(pipeline)!r}.[/red]")
        raise typer.Exit(code=1) from None
    finally:
        engine.dispose()  # type: ignore[attr-defined]
    console.print(f"[yellow]paused[/yellow] schedule for {escape(pipeline)}.")
    raise typer.Exit(code=0)


def resume_command(
    pipeline: str = typer.Argument(..., help="Pipeline to resume."),
    reason: str | None = typer.Option(None, "--reason", help="Why the schedule is resumed."),
) -> None:
    """Resume a paused schedule (audited)."""
    schedules, engine = _build_schedules()
    try:
        schedules.resume(pipeline, reason=reason, source="cli", actor_token_id=None)
    except ScheduleNotFound:
        console.print(f"[red]No schedule for pipeline {escape(pipeline)!r}.[/red]")
        raise typer.Exit(code=1) from None
    finally:
        engine.dispose()  # type: ignore[attr-defined]
    console.print(f"[green]resumed[/green] schedule for {escape(pipeline)}.")
    raise typer.Exit(code=0)


def set_cron_command(
    pipeline: str = typer.Argument(..., help="Pipeline whose cron to set."),
    cron: str = typer.Argument(..., help='New cron expression, e.g. "*/5 * * * *".'),
    timezone: str | None = typer.Option(
        None, "--timezone", help="IANA timezone for the cron (e.g. America/New_York)."
    ),
    target: str | None = typer.Option(
        None, "--target-pipeline", help="Target for a newly-created schedule (default: prod)."
    ),
    reason: str | None = typer.Option(None, "--reason", help="Why the cron changed."),
) -> None:
    """Set (or create) a schedule's cron — takes effect within one loop interval.

    Validates the cron + timezone up front (exit 2 on a bad value). UPSERTs: if no
    schedule exists for the pipeline it is created (so a schedule can be stood up
    end-to-end without the deferred reconciler-seed).
    """
    if not is_valid_cron(cron):
        console.print(
            f"[red]Invalid cron expression {escape(cron)!r}.[/red] "
            "Use a 5-field cron, e.g. `0 2 * * *` (2am daily)."
        )
        raise typer.Exit(code=2)
    if timezone is not None and not is_valid_timezone(timezone):
        console.print(
            f"[red]Unknown timezone {escape(timezone)!r}.[/red] "
            "Use an IANA name, e.g. `America/New_York` or `UTC`."
        )
        raise typer.Exit(code=2)

    schedules, engine = _build_schedules()
    try:
        sched = schedules.set_cron(
            pipeline,
            cron,
            target=target,
            timezone=timezone,
            reason=reason,
            source="cli",
            actor_token_id=None,
        )
    except CronError as exc:
        # Grammatically valid (`is_valid_cron` passed) but UNSATISFIABLE — e.g.
        # `0 0 30 2 *` (Feb 30). The tick is computed before the txn, so nothing
        # was persisted; surface a clean exit 2 instead of a raw traceback.
        console.print(f"[red]{escape(str(exc))}.[/red] The expression never matches a real date.")
        raise typer.Exit(code=2) from exc
    finally:
        engine.dispose()  # type: ignore[attr-defined]
    console.print(
        f"[green]set cron[/green] for {escape(pipeline)} → "
        f"{escape(sched.cron)} ({escape(sched.timezone)}); "
        f"next fire {_fmt(sched.next_fires_at)}."
    )
    raise typer.Exit(code=0)


__all__ = [
    "list_command",
    "pause_command",
    "resume_command",
    "set_cron_command",
    "show_command",
]
