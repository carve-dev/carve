"""``carve el run`` — execute an EL artifact against the active target.

Resolves the artifact under ``el/<name>/`` (flat layout, P1.1-01) and
runs it through ``LocalVenvRunner``. ``--target X`` overrides the
active target; ``--watch`` re-runs on filesystem changes (debounced
~300ms).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import typer
from rich.console import Console

from carve.cli.orchestrator import run_pipeline_by_name
from carve.core.config import ConfigError, load_config
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

if TYPE_CHECKING:
    from carve.core.config import Config

logger = logging.getLogger(__name__)

console = Console()


# Debounce window for the --watch loop. Mirrors the spec's 300ms.
_WATCH_DEBOUNCE_SECONDS: float = 0.3


def command(
    name: str = typer.Argument(
        ...,
        help="EL artifact name (directory under el/).",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help="Override the active target (defaults to carve.toml's default_target).",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Re-run on filesystem changes under the artifact directory.",
    ),
    no_auto_fix: bool = typer.Option(
        False,
        "--no-auto-fix",
        help="Disable the recovery agent; fail fast on the first error.",
    ),
    max_fix_attempts: int | None = typer.Option(
        None,
        "--max-fix-attempts",
        help=(
            "Override the per-failure recovery attempt budget (default "
            "from carve/runner.toml's [auto_fix] max_attempts)."
        ),
    ),
) -> None:
    """Run an EL artifact."""
    project_dir = Path.cwd()

    # Combine top-level ``carve --target X`` with subcommand-level
    # ``--target X``. Without this the subcommand-level None silently
    # wins and ``carve --target staging el run iowa`` runs against dev.
    from carve.cli.commands.el import resolve_subcommand_target

    target = resolve_subcommand_target(target)

    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    repository = Repository(session_factory)

    auto_fix_enabled = (not no_auto_fix) and config.runner.auto_fix.enabled

    try:
        if watch:
            exit_code = _run_with_watch(
                name=name,
                target=target,
                config=config,
                project_dir=project_dir,
                repository=repository,
                auto_fix=auto_fix_enabled,
                max_fix_attempts=max_fix_attempts,
            )
        else:
            exit_code = run_pipeline_by_name(
                pipeline_name=name,
                config=config,
                project_dir=project_dir,
                repository=repository,
                console=console,
                target=target,
                auto_fix=auto_fix_enabled,
                max_fix_attempts=max_fix_attempts,
            )
    finally:
        engine.dispose()

    raise typer.Exit(code=exit_code)


# ---------------------------------------------------------------------------
# --watch loop
# ---------------------------------------------------------------------------


class _WatchLoopExit(Exception):
    """Raised by the watch loop's signal handler to break out cleanly."""


class _ObserverProtocol(Protocol):
    """Structural type for a watchdog ``Observer`` (real or fake)."""

    def schedule(
        self, handler: object, path: str, recursive: bool = False
    ) -> object: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...


def _run_with_watch(
    *,
    name: str,
    target: str | None,
    config: Config,
    project_dir: Path,
    repository: Repository,
    observer_factory: Callable[[], _ObserverProtocol] | None = None,
    stop_event: threading.Event | None = None,
    auto_fix: bool = False,
    max_fix_attempts: int | None = None,
) -> int:
    """Run the artifact in a loop, re-running on filesystem changes.

    The observer is constructed via ``observer_factory`` so tests can
    inject a synchronous fake. Production callers leave it as ``None``
    and the function falls back to ``watchdog.observers.Observer``.

    ``stop_event`` is an optional escape hatch for tests: when set, the
    watch loop breaks without raising ``KeyboardInterrupt``. Production
    callers leave it ``None`` and rely on Ctrl-C through the terminal.
    """
    from carve.core.targets.resolution import (
        TargetResolutionError,
        resolve_active_target,
    )

    # Validate the active-target resolution up front so the watcher
    # doesn't spin up before we know the target is configured.
    try:
        resolve_active_target(target, config)
    except TargetResolutionError as exc:
        console.print(f"[red]✗[/red] {exc}")
        return 2

    artifact_dir = (project_dir / "el" / name).resolve()

    # `last_exit_code = -1` is the sentinel for "watch loop broke before
    # any iteration ran" (e.g. tests that prime ``stop_event`` before
    # invoking). The first successful iteration overwrites it with the
    # actual run exit code.
    last_exit_code = -1

    # Trigger event used by the file-change handler to wake the loop.
    trigger = threading.Event()

    handler = _DebouncedHandler(trigger=trigger, debounce=_WATCH_DEBOUNCE_SECONDS)

    observer = _make_observer(observer_factory)
    if artifact_dir.is_dir():
        # Defense-in-depth: refuse to schedule the watcher on a path that
        # — after symlink resolution — has escaped the project root. The
        # path is already resolved above; this guard catches the case
        # where ``el/<name>/`` is itself a symlink to somewhere outside
        # the repo. Each loop iteration re-validates via
        # ``run_pipeline_by_name``, so an escape here is benign in
        # practice — but the watcher would still fire on out-of-tree
        # file events, and the cheaper fix is to refuse up front.
        try:
            artifact_dir.relative_to(project_dir.resolve())
        except ValueError:
            console.print(
                f"[red]✗[/red] artifact directory {artifact_dir} resolves "
                f"outside the project root; refusing to watch."
            )
            return 2
        # Shallow watch: the artifact directory only. Cross-target dev
        # work runs --watch separately per artifact.
        observer.schedule(handler, str(artifact_dir), recursive=False)
    observer.start()

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            last_exit_code = run_pipeline_by_name(
                pipeline_name=name,
                config=config,
                project_dir=project_dir,
                repository=repository,
                console=console,
                target=target,
                auto_fix=auto_fix,
                max_fix_attempts=max_fix_attempts,
            )
            console.print(
                f"[dim]\\[watching {artifact_dir.relative_to(project_dir)} — "
                f"Ctrl-C to exit][/dim]"
            )
            try:
                # `Event.wait` is interruptible by KeyboardInterrupt, so
                # Ctrl-C drops out cleanly. Tests use ``stop_event``.
                if stop_event is not None:
                    while not trigger.is_set() and not stop_event.is_set():
                        # Tick on a short timeout so the stop signal is
                        # checked even when no file event has fired.
                        trigger.wait(timeout=0.05)
                    if stop_event.is_set():
                        break
                else:
                    trigger.wait()
                trigger.clear()
            except KeyboardInterrupt:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            observer.stop()
            observer.join(timeout=1.0)
        except Exception:
            logger.exception("watchdog observer shutdown failed")

    return last_exit_code


def _make_observer(
    observer_factory: Callable[[], _ObserverProtocol] | None,
) -> _ObserverProtocol:
    """Construct a watchdog Observer (or test-injected fake)."""
    if observer_factory is not None:
        return observer_factory()
    from watchdog.observers import Observer

    # `watchdog.observers.Observer` is structurally compatible with our
    # `_ObserverProtocol`. Cast for mypy.
    return Observer()  # type: ignore[return-value]


class _DebouncedHandler:
    """A watchdog `FileSystemEventHandler` that fires `trigger` after a debounce.

    Implementing the protocol structurally rather than subclassing
    `FileSystemEventHandler` keeps the watchdog import inside
    ``_make_observer`` so unit tests don't need watchdog at import
    time.
    """

    def __init__(self, *, trigger: threading.Event, debounce: float) -> None:
        self._trigger = trigger
        self._debounce = debounce
        self._lock = threading.Lock()
        self._pending_timer: threading.Timer | None = None

    def dispatch(self, event: object) -> None:
        """Called by watchdog for every event; debounce and fire."""
        # Ignore directory events; we only care about file changes.
        is_directory = bool(getattr(event, "is_directory", False))
        if is_directory:
            return
        self._schedule_fire()

    # Watchdog calls these directly when not using `dispatch`.
    def on_modified(self, event: object) -> None:
        self.dispatch(event)

    def on_created(self, event: object) -> None:
        self.dispatch(event)

    def on_deleted(self, event: object) -> None:
        self.dispatch(event)

    def on_moved(self, event: object) -> None:
        self.dispatch(event)

    def _schedule_fire(self) -> None:
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
            timer = threading.Timer(self._debounce, self._fire)
            timer.daemon = True
            self._pending_timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._pending_timer = None
        self._trigger.set()


# Test seam: a synchronous "fake observer" that lets tests fire events
# without real filesystem activity. Production code never uses this.
class _SyncObserver:
    """In-process observer used by the unit tests.

    ``schedule`` records the handler; tests then call ``fire(event)``
    to invoke the handler's ``dispatch`` method directly. No threads,
    no real filesystem I/O.
    """

    def __init__(self) -> None:
        self._handlers: list[object] = []

    def schedule(
        self, handler: object, path: str, recursive: bool = False
    ) -> object:
        # `path` and `recursive` are accepted to match watchdog's API
        # but ignored — the test fires events directly via the handler.
        del path, recursive
        self._handlers.append(handler)
        return handler

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def join(self, timeout: float | None = None) -> None:
        del timeout

    def fire(self, event: object) -> None:
        for handler in self._handlers:
            dispatch = getattr(handler, "dispatch", None)
            if dispatch is not None:
                dispatch(event)


# Only `command` is part of the supported public surface. The other
# names are documented test seams and remain importable as
# ``el_run._SyncObserver`` etc., but they aren't advertised here so the
# leading-underscore privacy signal is consistent.
__all__ = ["command"]
