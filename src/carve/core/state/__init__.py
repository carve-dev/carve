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
from carve.core.state.models import Base, Build, Log, Pipeline, Plan, Run
from carve.core.state.repository import Repository

__all__ = [
    "Base",
    "Build",
    "Log",
    "Pipeline",
    "Plan",
    "Repository",
    "Run",
    "create_engine_from_config",
    "create_session_factory",
    "initialize_database",
]
