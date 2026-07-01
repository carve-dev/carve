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
from carve.core.state.events_read import EventsReader
from carve.core.state.idempotency import IdempotencyKeys
from carve.core.state.models import (
    Base,
    Build,
    IdempotencyKey,
    Job,
    Log,
    Pipeline,
    Plan,
    Run,
    Schedule,
    ScheduleChange,
    StepRun,
    Token,
    Webhook,
    WebhookDelivery,
    Worker,
    Workspace,
)
from carve.core.state.repository import Repository
from carve.core.state.schedules import Schedules
from carve.core.state.store import StateStore
from carve.core.state.tokens import Identity, Tokens
from carve.core.state.webhooks import WebhookDeliveries, Webhooks

__all__ = [
    "Base",
    "Build",
    "EventsReader",
    "IdempotencyKey",
    "IdempotencyKeys",
    "Identity",
    "Job",
    "Log",
    "Pipeline",
    "Plan",
    "Repository",
    "Run",
    "Schedule",
    "ScheduleChange",
    "Schedules",
    "StateStore",
    "StepRun",
    "Token",
    "Tokens",
    "Webhook",
    "WebhookDeliveries",
    "WebhookDelivery",
    "Webhooks",
    "Worker",
    "Workspace",
    "create_engine_from_config",
    "create_session_factory",
    "initialize_database",
]
