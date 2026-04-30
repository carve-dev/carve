"""Runners: the components that execute steps.

Public surface:

- `Runner`, `RunHandle`, `LogLine` — the protocol every runner implements.
- `LocalVenvRunner` — the M1 OSS runner that uses local virtualenvs.

The runner protocol is non-blocking on `execute()` and blocking on
`wait()`, so the future API server can spawn work and return 200
immediately while the actual run continues in the background.
"""

from carve.core.runners.base import LogLine, RunHandle, Runner
from carve.core.runners.local_venv import LocalVenvRunner

__all__ = [
    "LocalVenvRunner",
    "LogLine",
    "RunHandle",
    "Runner",
]
