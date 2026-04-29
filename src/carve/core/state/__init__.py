"""State store: SQL persistence for runs, logs, and plans.

Public surface:

- `Repository` — typed access methods, the only module that issues SQL.
- `Run`, `Log`, `Plan` — ORM models, also returned by `Repository`.
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
from carve.core.state.models import Base, Log, Plan, Run
from carve.core.state.repository import Repository

__all__ = [
    "Base",
    "Log",
    "Plan",
    "Repository",
    "Run",
    "create_engine_from_config",
    "create_session_factory",
    "initialize_database",
]
