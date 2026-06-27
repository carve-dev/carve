"""State store: SQL persistence for runs, logs, plans, and pipelines.

Public surface:

- `Repository` — typed access methods, the only module that issues SQL.
- `Run`, `Log`, `Plan`, `Pipeline` — ORM models, also returned by `Repository`.
- `create_engine_from_config`, `create_session_factory`, `initialize_database`
  — engine/session helpers wired from a `Config`.

CLI commands, agents, and runners construct a `Repository` and call its
methods; they never touch a `Session` directly.
"""

from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import (
    Base,
    Build,
    Job,
    Log,
    Pipeline,
    Plan,
    Run,
    Schedule,
    ScheduleChange,
    StepRun,
    Worker,
    Workspace,
)
from carve.core.state.repository import Repository
from carve.core.state.schedules import Schedules

__all__ = [
    "Base",
    "Build",
    "Job",
    "Log",
    "Pipeline",
    "Plan",
    "Repository",
    "Run",
    "Schedule",
    "ScheduleChange",
    "Schedules",
    "StepRun",
    "Worker",
    "Workspace",
    "create_engine_from_config",
    "create_session_factory",
    "initialize_database",
]
