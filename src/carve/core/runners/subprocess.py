"""Shared subprocess primitive — run a command to completion, safely.

The dbt-execution ``local`` backend needs the exact subprocess discipline
:class:`carve.core.runners.local_venv.LocalVenvRunner` already encodes — own
process group, SIGTERM→grace→SIGKILL cancellation, a wall-clock watchdog, and
the Carve-internal secret env vars stripped from the child — but in a
*run-to-completion* shape (capture all output, return when done) rather than the
streaming, state-store-driven shape ``LocalVenvRunner`` uses for ``PythonStep``.

Rather than overload ``LocalVenvRunner`` (which is ``PythonStep``-typed and
repo-driven) or hand-roll new process management, this module factors the
process primitive into one place both can share. ``LocalVenvRunner`` keeps its
streaming path; this primitive serves the backend's synchronous run + parse.

Secret stripping and the ``_killpg`` group-signalling helper are imported from
``local_venv`` so there is a single source of truth for "what Carve never leaks
into a child" and "how Carve kills a process group".
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from carve.core.runners.local_venv import _STRIPPED_ENV_VARS, _killpg

_KILL_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class CompletedSubprocess:
    """The finished result of a run-to-completion subprocess.

    ``output`` is the combined stdout+stderr (stderr merged into stdout so log
    ordering matches a terminal). ``timed_out`` is True iff the watchdog killed
    it for exceeding the wall-clock budget.
    """

    returncode: int
    output: str
    timed_out: bool


class Subprocess:
    """Run a command to completion with Carve's subprocess discipline."""

    @staticmethod
    def run_to_completion(
        argv: list[str],
        *,
        cwd: str | Path,
        timeout_seconds: int,
        extra_env: dict[str, str] | None = None,
    ) -> CompletedSubprocess:
        """Spawn ``argv`` in its own process group, capture output, wait.

        * The child runs in its own session/process group
          (``start_new_session=True``) so a watchdog timeout signals the whole
          tree (any children the command forks).
        * The Carve-internal secrets (``ANTHROPIC_API_KEY`` /
          ``ANTHROPIC_AUTH_TOKEN``) are **stripped** from the child env;
          ``extra_env`` is layered on top.
        * A wall-clock watchdog enforces ``timeout_seconds`` (SIGTERM → grace →
          SIGKILL); ``timeout_seconds <= 0`` disables it.

        Returns once the process exits, with combined stdout/stderr captured.
        """
        env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV_VARS}
        if extra_env:
            env.update(extra_env)

        proc = subprocess.Popen(
            [str(a) for a in argv],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(cwd),
            start_new_session=True,
        )

        timed_out = threading.Event()
        watchdog = _start_watchdog(proc, timeout_seconds, timed_out)
        try:
            stdout_bytes, _ = proc.communicate()
        finally:
            if watchdog is not None:
                watchdog.cancel()

        output = (stdout_bytes or b"").decode("utf-8", errors="replace")
        return CompletedSubprocess(
            returncode=proc.returncode,
            output=output,
            timed_out=timed_out.is_set(),
        )


def _start_watchdog(
    proc: subprocess.Popen[bytes],
    timeout_seconds: int,
    timed_out: threading.Event,
) -> threading.Timer | None:
    """Arm a wall-clock timer that SIGTERM→grace→SIGKILLs the process group."""
    if timeout_seconds <= 0:
        return None

    def _kill() -> None:
        if proc.poll() is not None:
            return
        timed_out.set()
        _killpg(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _killpg(proc, signal.SIGKILL)

    timer = threading.Timer(timeout_seconds, _kill)
    timer.daemon = True
    timer.start()
    return timer


__all__ = ["CompletedSubprocess", "Subprocess"]
