"""The M1 OSS runner: execute Python steps in a local virtualenv.

`LocalVenvRunner` materialises a venv per unique requirements set,
caches it on disk, and spawns the user's script as a subprocess. Output
is streamed line-by-line to the state store so the CLI's ``carve logs``
view (M1) and the future WebSocket layer (M2) can read from one source.

Three threads are involved per run:

1. The main thread that called `execute()` — returns immediately with a
   `RunHandle` once the subprocess is spawned.
2. A daemon log-streaming thread that reads ``proc.stdout`` line by
   line, appends each line to the repository, and finalises the run
   row when the subprocess exits.
3. A daemon watchdog thread that sleeps for ``timeout_seconds`` and
   then calls `cancel()` if the run is still active.

Cancellation is SIGTERM, wait 5 seconds, SIGKILL. The watchdog and
streamer are both daemons so test processes don't hang on stale work.

Path-traversal on `script` is rejected: the resolved script must live
inside ``context.project_dir``.

Snowflake credentials live on the top-level `Config`, not on
`RunnerConfig`, so we pull them from `RunContext.config` rather than
``self.config``. The spec's pseudocode (``self.config.connections...``)
referred to a field that doesn't exist; this module is the source of
truth for the right shape.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import AsyncIterator
from pathlib import Path

from carve.core.config import RunnerConfig, SnowflakeConnection
from carve.core.runners.base import LogLine, RunHandle
from carve.core.state import Repository
from carve.core.steps.base import RunContext, StepResult
from carve.core.steps.python import PythonStep

_KILL_GRACE_SECONDS = 5.0

# Carve-internal env vars that must NOT be inherited by the user
# subprocess. The Anthropic API key is loaded into this process so the
# Carve agents can call the API; user scripts (which may be LLM-authored)
# must not see it. Add additional internal-only secret env vars here.
_STRIPPED_ENV_VARS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    }
)

_logger = logging.getLogger(__name__)


class LocalVenvRunner:
    """Local-venv subprocess runner for `PythonStep`."""

    def __init__(
        self,
        config: RunnerConfig,
        repo: Repository,
        *,
        python_executable: str | None = None,
    ) -> None:
        """Create a runner.

        `python_executable` is the interpreter used to *create* venvs.
        Tests inject ``sys.executable`` (the default) so they don't
        need pip access; production also defaults to ``sys.executable``
        which is the right answer 99% of the time. Override only if the
        host has multiple Pythons and the user picked one explicitly.
        """
        self.config = config
        self.repo = repo
        self.python_executable: str = python_executable or sys.executable
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._start_times: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ Public

    def execute(self, step: PythonStep, context: RunContext) -> RunHandle:
        """Start executing `step` in the background.

        Returns a `RunHandle` once the subprocess is spawned. The run
        row is moved to ``running`` here; the streamer thread finalises
        it (``success`` / ``failed``) when the subprocess exits.

        Raises `ValueError` if `step.config.script` resolves outside
        `context.project_dir`.
        """
        run_id = context.run_id

        venv_path = self._ensure_venv(step.config.requirements)
        python = self._venv_python(venv_path)

        script_abs = self._resolve_script(context.project_dir, step.config.script)

        # Strip Carve-internal secrets (Anthropic key etc.) before the
        # user script can read them. A step that legitimately needs one
        # of these can opt back in via `step.config.env`, which is
        # layered on top below.
        env = {
            k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV_VARS
        }
        env.update(step.config.env)
        env.update(_snowflake_env(context))

        # stderr is merged into stdout so log ordering matches what the
        # user would see in a terminal. We read in binary (bufsize=0,
        # readline-driven) and decode per line; line-buffering on the
        # binary stream is unsupported in CPython.
        #
        # `start_new_session=True` puts the child in its own process
        # group so cancellation can signal the whole tree (including
        # any subprocesses the user script forks). Without it, forks
        # of the user script survive `cancel()` and keep running with
        # Snowflake creds in their env.
        proc = subprocess.Popen(
            [str(python), str(script_abs)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(context.project_dir),
            start_new_session=True,
        )

        with self._lock:
            self._processes[run_id] = proc
            self._start_times[run_id] = time.monotonic()

        self.repo.update_run_status(run_id, "running")

        threading.Thread(
            target=self._stream_logs_to_repo,
            args=(run_id, proc),
            daemon=True,
            name=f"carve-log-stream-{run_id}",
        ).start()

        threading.Thread(
            target=self._watchdog,
            args=(run_id, proc, step.config.timeout_seconds),
            daemon=True,
            name=f"carve-watchdog-{run_id}",
        ).start()

        return RunHandle(run_id=run_id, process_id=proc.pid)

    async def stream_logs(self, run_id: str) -> AsyncIterator[LogLine]:
        """Async-iterate over log lines as they arrive.

        Polls the repository every 250ms for new lines since the last
        seen log id. Stops once the run reaches a terminal state and
        no further log lines appear.

        Cursor is the autoincrement primary key (`Log.id`), not the
        wall-clock timestamp: lines appended within the same
        millisecond share a timestamp and would be missed by a
        timestamp-based filter, dropping the trailing logs of fast
        runs.

        For M1 this is the simplest correct implementation; M2's
        WebSocket layer will replace polling with a notification feed.
        """
        terminal = {"success", "failed", "cancelled", "crashed"}
        last_seen_id: int | None = None
        while True:
            logs = self.repo.get_logs(run_id, since_id=last_seen_id)
            for log in logs:
                yield LogLine(
                    run_id=run_id,
                    level=log.level,
                    source=log.source,
                    message=log.message,
                )
            if logs:
                last_seen_id = max(log.id for log in logs)
            run = self.repo.get_run(run_id)
            if run is not None and run.status in terminal and not logs:
                return
            await asyncio.sleep(0.25)

    def get_status(self, run_id: str) -> str:
        """Return the current run status from the state store."""
        run = self.repo.get_run(run_id)
        if run is None:
            return "unknown"
        return run.status

    def cancel(self, run_id: str) -> None:
        """Cancel a running step.

        Sends SIGTERM to the subprocess's *process group* (so forked
        children die too), waits up to 5 seconds, then escalates to
        SIGKILL. Idempotent — a no-op if the run is already finished.
        """
        with self._lock:
            proc = self._processes.get(run_id)
        if proc is None or proc.poll() is not None:
            return

        _killpg(proc, signal.SIGTERM)

        try:
            proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _killpg(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass

    def wait(self, run_id: str) -> StepResult:
        """Block until the step completes; return the result."""
        with self._lock:
            proc = self._processes.get(run_id)
        if proc is not None:
            proc.wait()

        # The streamer thread is the source of truth for finalising the
        # row, but it may not have run yet. Spin briefly until it does.
        deadline = time.monotonic() + 5.0
        terminal = {"success", "failed", "cancelled", "crashed"}
        while time.monotonic() < deadline:
            run = self.repo.get_run(run_id)
            if run is not None and run.status in terminal:
                return StepResult(
                    status=run.status,
                    duration_ms=run.duration_ms or 0,
                    error=run.error_message,
                )
            time.sleep(0.05)

        # Fallback — return whatever we have.
        run = self.repo.get_run(run_id)
        if run is None:
            return StepResult(status="failed", duration_ms=0, error="run row missing")
        return StepResult(
            status=run.status,
            duration_ms=run.duration_ms or 0,
            error=run.error_message,
        )

    # --------------------------------------------------------------- Internals

    def _ensure_venv(self, requirements: list[str]) -> Path:
        """Materialise a venv for `requirements`, caching by hash.

        The cache key is the full SHA-256 hex digest of the sorted
        requirements list, so an empty list is still a stable, valid
        path (handy for tests and for the trivial-script case). If the
        directory already exists we trust it — invalidation is implicit
        via a different hash when requirements change.
        """
        req_hash = hashlib.sha256(
            "\n".join(sorted(requirements)).encode("utf-8")
        ).hexdigest()

        cache_root = Path(self.config.venv_cache_dir)
        if not cache_root.is_absolute():
            cache_root = (Path.cwd() / cache_root).resolve()
        venv_dir = cache_root / req_hash

        if venv_dir.exists():
            return venv_dir

        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(
            [self.python_executable, "-m", "venv", str(venv_dir)]
        )

        if requirements:
            pip = self._venv_pip(venv_dir)
            # `--` terminates pip's flag parsing so requirement entries
            # can never be interpreted as flags (e.g. ``--index-url=...``).
            # `PythonStepConfig.requirements` also rejects flag-shaped
            # entries at validation time; this is belt-and-braces.
            subprocess.check_call(
                [str(pip), "install", "--", *requirements]
            )

        return venv_dir

    def _stream_logs_to_repo(
        self,
        run_id: str,
        proc: subprocess.Popen[bytes],
    ) -> None:
        """Daemon thread: drain stdout, write lines to the repo, finalise."""
        try:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                decoded = raw.decode("utf-8", errors="replace").rstrip()
                if not decoded:
                    continue
                try:
                    self.repo.append_log(
                        run_id=run_id,
                        level="info",
                        source="runner",
                        message=decoded,
                    )
                except Exception:
                    # The streamer must never crash (it owns the only
                    # path to finalise the run row). Log the failure
                    # so a broken state store is at least debuggable
                    # rather than silently swallowed.
                    _logger.exception(
                        "append_log failed for run_id=%s", run_id
                    )
                    print(
                        f"carve.runners.local_venv: append_log failed "
                        f"for run_id={run_id}",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                _logger.exception(
                    "closing stdout failed for run_id=%s", run_id
                )
            proc.wait()
            self._finalize_run(run_id, proc.returncode)

    def _finalize_run(self, run_id: str, exit_code: int) -> None:
        """Move the run row to its terminal state.

        Also clears the run from the in-memory tracking dicts so the
        watchdog (if still running) becomes a no-op.
        """
        with self._lock:
            self._processes.pop(run_id, None)
            self._start_times.pop(run_id, None)

        # If something else (e.g. cancel) already moved us to a
        # non-active terminal state, leave it alone.
        run = self.repo.get_run(run_id)
        if run is not None and run.status in {"cancelled", "crashed"}:
            return

        if exit_code == 0:
            self.repo.update_run_status(run_id, "success")
            return

        # Negative exit codes on POSIX = killed by signal N (where N = -code).
        if exit_code < 0:
            sig = -exit_code
            self.repo.update_run_status(
                run_id,
                "cancelled",
                error=f"terminated by signal {sig}",
            )
            return

        self.repo.update_run_status(
            run_id,
            "failed",
            error=f"script exited with code {exit_code}",
        )

    def _watchdog(
        self,
        run_id: str,
        proc: subprocess.Popen[bytes],
        timeout_seconds: int,
    ) -> None:
        """Daemon thread: enforce the per-step wall-clock timeout.

        Captures the `Popen` object at registration and verifies object
        identity before cancelling, so a recycled run id can't trigger
        a stale watchdog against a different process.
        """
        if timeout_seconds <= 0:
            return
        time.sleep(timeout_seconds)
        with self._lock:
            tracked = self._processes.get(run_id)
        if tracked is proc:
            self.cancel(run_id)

    @staticmethod
    def _resolve_script(project_dir: Path, script: str) -> Path:
        """Resolve `script` against `project_dir`, rejecting traversal.

        Both paths are resolved (symlinks followed) before comparison so
        a symlink inside the project that points outside is also caught.
        """
        project_resolved = project_dir.resolve()
        candidate = (project_resolved / script).resolve()
        try:
            candidate.relative_to(project_resolved)
        except ValueError as exc:
            raise ValueError(
                f"script path {script!r} resolves outside project_dir"
            ) from exc
        return candidate

    @staticmethod
    def _venv_python(venv_dir: Path) -> Path:
        """Path to the venv's Python interpreter (POSIX or Windows)."""
        if os.name == "nt":  # pragma: no cover - tests run on POSIX
            return venv_dir / "Scripts" / "python.exe"
        return venv_dir / "bin" / "python"

    @staticmethod
    def _venv_pip(venv_dir: Path) -> Path:
        """Path to the venv's pip (POSIX or Windows)."""
        if os.name == "nt":  # pragma: no cover - tests run on POSIX
            return venv_dir / "Scripts" / "pip.exe"
        return venv_dir / "bin" / "pip"


def _killpg(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Send `sig` to the subprocess's process group, tolerating races.

    The subprocess is started with `start_new_session=True`, so its
    pid is also its process-group id. Signalling the group means any
    children the user script forked die too.

    `ProcessLookupError` (the group has already exited) and `EPERM`
    (we don't have permission to signal a member, which can happen
    if a child re-parented to init) are both swallowed: cancellation
    is best-effort and idempotent.
    """
    if not hasattr(os, "killpg"):  # pragma: no cover - Windows only
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        if exc.errno == errno.EPERM:
            return
        raise
    except OSError as exc:
        if exc.errno in (errno.ESRCH, errno.EPERM):
            return
        raise


def _snowflake_env(context: RunContext) -> dict[str, str]:
    """Build the Snowflake env-var dict for `context.target`.

    Pulled out of the class so the resolution logic — "find the named
    target on the full `Config`, not on the `RunnerConfig`" — is
    explicit and testable. Returns an empty dict if the target isn't
    configured (the agent's script can decide whether that's an error).
    """
    snowflake = context.config.connections.snowflake
    sf: SnowflakeConnection | None = snowflake.get(context.target)
    if sf is None:
        return {}
    return {
        "SNOWFLAKE_ACCOUNT": sf.account,
        "SNOWFLAKE_USER": sf.user,
        "SNOWFLAKE_PASSWORD": sf.password or "",
        "SNOWFLAKE_ROLE": sf.role,
        "SNOWFLAKE_WAREHOUSE": sf.warehouse,
        "SNOWFLAKE_DATABASE": sf.database,
        "SNOWFLAKE_SCHEMA": sf.schema_ or "",
    }
