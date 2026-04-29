# M1-03 — State store

**Milestone:** 1 — Walking skeleton
**Estimated effort:** 0.5 day
**Dependencies:** M1-02 (config loader provides the connection string)

## Purpose

Persist Carve's operational state — runs, logs, plans — in a SQL database so that the CLI, the future API server, and the future web UI can query it consistently. Use SQLite for OSS simplicity; structure the code so a Postgres migration is a connection string change.

## Scope

### In scope

- SQLAlchemy ORM models for `runs`, `logs`, `plans`
- Repository pattern: typed query methods, no raw SQL leaking out
- Alembic-style migrations (or a simpler equivalent)
- Database initialization on `carve init`
- A `Repository` class accessible from the CLI and (later) API server

### Out of scope

- Pipeline persistence (M2; pipelines live in TOML files for now)
- Step-level state (M3, when multi-step pipelines arrive)
- Schedule persistence (M2)
- Artifact registry (M2 for PR tracking)
- Event log persistence (M2 for the event bus)

## Data model for M1

Three tables. Schema is intentionally minimal; M2 and M3 add columns.

### `runs`

A single execution of a plan or pipeline.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT (UUID) | Primary key |
| `kind` | TEXT | `apply` or `pipeline` |
| `target_id` | TEXT | Plan ID or pipeline name |
| `owner_user_id` | INT | Always `1` for single-user mode |
| `status` | TEXT | `queued`, `running`, `success`, `failed`, `cancelled`, `crashed` |
| `started_at` | DATETIME | nullable until run starts |
| `completed_at` | DATETIME | nullable until run completes |
| `duration_ms` | INT | Computed at completion |
| `error_message` | TEXT | nullable |
| `tokens_input` | INT | Sum across all agent invocations |
| `tokens_output` | INT | Sum across all agent invocations |
| `cost_usd` | REAL | Computed from tokens |
| `created_at` | DATETIME | Insert time |

### `logs`

Streamed log lines from runs.

| Column | Type | Notes |
|---|---|---|
| `id` | INT | Auto-increment primary key |
| `run_id` | TEXT | Foreign key to `runs.id` |
| `timestamp` | DATETIME | When the line was emitted |
| `level` | TEXT | `debug`, `info`, `warn`, `error` |
| `source` | TEXT | `agent`, `runner`, `system` |
| `message` | TEXT | The log line |

Index on `(run_id, timestamp)` for efficient log-tail queries.

### `plans`

Saved plan files referenced from disk; this table is the index.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | Plan ID, matches the file name |
| `parent_plan_id` | TEXT | Nullable; for refined plans |
| `goal` | TEXT | The original natural-language goal |
| `config_hash` | TEXT | The config hash at generation time |
| `carve_version` | TEXT | Version that generated the plan |
| `estimates_json` | TEXT | Cost, duration estimates (JSON blob) |
| `task_graph_json` | TEXT | The task graph (JSON blob) |
| `file_path` | TEXT | Path to the JSON file on disk |
| `created_at` | DATETIME | |
| `expires_at` | DATETIME | Default 24h after `created_at` |
| `applied_at` | DATETIME | Nullable; set on first apply |
| `apply_run_id` | TEXT | Nullable; foreign key to `runs.id` |

The actual plan JSON is on disk at `.carve/plans/<id>.json`. The DB row is a queryable index.

## Implementation

### File: `src/carve/core/state/models.py`

SQLAlchemy 2.0+ declarative models:

```python
from datetime import datetime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str]
    target_id: Mapped[str]
    owner_user_id: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="queued")
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    duration_ms: Mapped[int | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    tokens_input: Mapped[int] = mapped_column(default=0)
    tokens_output: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

# ... Log, Plan classes follow same pattern
```

### File: `src/carve/core/state/repository.py`

Typed access methods:

```python
class Repository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    # Runs
    def create_run(self, kind: str, target_id: str) -> str: ...
    def update_run_status(self, run_id: str, status: str, error: str | None = None): ...
    def get_run(self, run_id: str) -> Run | None: ...
    def list_runs(self, status: str | None = None, limit: int = 50) -> list[Run]: ...

    # Logs
    def append_log(self, run_id: str, level: str, source: str, message: str): ...
    def get_logs(self, run_id: str, since: datetime | None = None) -> list[Log]: ...

    # Plans
    def save_plan(self, plan: Plan): ...
    def get_plan(self, plan_id: str) -> Plan | None: ...
    def list_plans(self, limit: int = 50) -> list[Plan]: ...
    def expire_old_plans(self): ...
```

The repository is the only component that talks to the database. Higher layers (CLI commands, agents, runners) use the repository.

### File: `src/carve/core/state/database.py`

Engine and session management:

```python
def create_engine_from_config(config: Config) -> Engine:
    return create_engine(config.server.state_store, echo=False)

def create_session_factory(engine: Engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

def initialize_database(engine: Engine):
    """Create tables if they don't exist. Called on `carve init`."""
    Base.metadata.create_all(engine)
```

### Migrations

For M1, just `Base.metadata.create_all()` is enough — no separate migration tool. As soon as M2 starts adding columns, introduce alembic migrations.

Plan: M1 ships with `create_all()`; the first M2 spec that needs a schema change introduces alembic and writes a baseline migration.

### Concurrency

SQLite is fine for our load (a single-engineer Carve has at most a handful of concurrent operations). To avoid lock contention:

- Open the database in WAL mode (`PRAGMA journal_mode=WAL`)
- Use `synchronous=NORMAL`
- Each repository method opens and commits its own transaction quickly

For Postgres later, none of this matters — it handles concurrency natively.

## Hooking into the CLI

`carve init` initializes the database:

```python
from carve.core.config import load_config
from carve.core.state.database import create_engine_from_config, initialize_database

def init_command(...):
    # ... create files ...
    config = load_config()
    engine = create_engine_from_config(config)
    initialize_database(engine)
```

The state directory `.carve/` should be created if it doesn't exist. SQLite will auto-create the file at the configured path.

## Tests

- Tables are created on init
- Creating a run returns an ID and persists the row
- Updating run status works
- Listing runs respects filters and limits
- Appending logs preserves order
- Plan save/get round-trip works
- Expired plans can be queried
- WAL mode is enabled

Use a `tmp_path` SQLite file per test for isolation.

## Acceptance criteria

- After `carve init`, `.carve/state.db` exists with the three tables
- `Repository` methods are fully typed and return Pydantic-friendly objects
- Tests pass with both SQLite (default) and Postgres (use `testcontainers` or skip in CI if unavailable)
- No raw SQL strings appear outside the repository module
- WAL mode is enabled

## Files this spec produces

- `src/carve/core/state/__init__.py`
- `src/carve/core/state/database.py`
- `src/carve/core/state/models.py`
- `src/carve/core/state/repository.py`
- `tests/core/state/test_database.py`
- `tests/core/state/test_repository.py`

## What this enables

- The agent loop (next spec) can record runs and logs
- The plan/apply workflow (M2) has a place to persist plans
- The web UI (M2) reads the state store directly via the API server
- Postgres migration later is a config change, not a rewrite
