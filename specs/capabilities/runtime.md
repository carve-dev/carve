# Runtime: scheduler, job queue, workers, heartbeats, reaper, archiver

> The largest net-new module. Ships the scheduling, queueing, worker process model, optimistic-claim semantics, crash recovery, and archive flow described in [ARCHITECTURE §4](../ARCHITECTURE.md). Implements the runtime that PRD's [§6.8 Scheduling](../PRD.md) and [§6.6 Run](../PRD.md) describe at the product level.

## Status

- **Status:** Drafting
- > **Lean first slice landed (2026-06-26).** The runtime is the largest net-new module; it ships in slices. **This first slice shipped the queue → run → persist loop**: migration `0008_runtime_queue` (`jobs` + `workers` + `step_runs`, with the two partial unique indexes); the **sync `JobQueue`** (`core/state/job_queue.py` — `enqueue_scheduled` ON-CONFLICT dedup, `enqueue_manual` upsert, `FOR UPDATE SKIP LOCKED` `claim_next`, `transition_to_running`/`mark_finished`/`release_claim`, worker register/unregister, `create_step_run`/`finish_step_run`); the **real persisting `StepSink`** (`runtime/persisting_step_sink.py` — fills the no-op seam pipelines forward-declared, so `step_runs` persist for the first time); a **minimal worker** (`runtime/worker.py` — `run_once`/`worker_loop`, claim-then-never-orphan); and **`carve worker`** (`cli/commands/worker.py`, `--once`/loop). **DEFERRED to later runtime slices** (each fenced inline below): the **scheduler loop + live `schedules` table + `carve schedule` CLI + `schedule_changes`**; the **heartbeat loop + reaper** (the `heartbeat_at` column ships and is stamped once at claim; the loops defer); the **archiver + `*_archive` tables**; the full **`carve serve`** supervisor + the **`events` table/emitter** (the `step.*` emit is a marked no-op seam); the **worker-pool fan-out** (`--workers N`); and **crash recovery**. Status stays **Drafting** — more slices remain.
- **Revised for the control-plane model** ([../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md), "Resolved design decisions (2026-06-16)"). The reconciler reconciles the pipeline *definition* only (steps, DAG, component refs, pins — owned by [pipelines](./pipelines.md)); the **schedule is data** — this spec's scheduler reads the `schedules` table as its source of truth, seeded once from a pipeline's optional `[seed_schedule]` block (the seed + `carve schedule reseed` live in [pipelines](./pipelines.md); this spec owns the live `schedules` table, the scheduler that reads it, and `carve schedule list/show/pause/resume/set-cron`). This **supersedes UC2's code-vs-runtime-override TTL-precedence machinery** (see [Design notes](#design-notes)). Deploy events are untouched (pending the Wave 2 deploy revision).
- **Depends on:** [state-store](./state-store.md) (partial unique indexes, FOR UPDATE SKIP LOCKED), [layout](./layout.md) (path resolution at run time)
- **Blocks:** [pipelines](./pipelines.md) (which ships the actual step-type implementations on top of this spec's executor framework), [rest-api](./rest-api.md), [ui](./ui.md)
- **Built on:** the `LocalVenvRunner` subprocess primitive from M1-05 (HISTORICAL — preserved). This spec wraps that primitive in a scheduler + queue + worker layer; M1-05's code is not replaced.

## Goal

Build the narrow, opinionated runtime that turns a pipeline's **live schedule row** (seeded once from its optional `[seed_schedule]` block, then owned as data) into scheduled runs with predictable execution semantics. Concretely:

1. **Scheduler** — a loop that reads the `schedules` table as its source of truth and fires due pipelines onto a Postgres-backed job queue
2. **Job queue** — schema-enforced "at most one queued and one running per pipeline" semantics via partial unique indexes
3. **Workers** — long-running processes that claim jobs via optimistic-claim semantics and execute them
4. **Heartbeats** — workers signal liveness every 10 seconds while holding a job
5. **Reaper** — reclaims jobs from workers whose heartbeat has gone stale
6. **Archiver** — moves completed rows from active tables to archive tables on a configurable window, with verify-then-delete safety
7. **Step executor framework** — the abstract interface that the three step types (`dlt`, `dbt`, `sql` — implemented in spec 08) plug into
8. **`carve serve` and `carve worker` CLI** — the two entry points users invoke to run Carve
9. **`carve schedule` CLI** — the live-data surface (`list/show/pause/resume/set-cron`) that mutates the `schedules` table instantly, with a `schedule_changes` audit trail

The runtime is deliberately narrow per design decision [5.6](../ARCHITECTURE.md): no asset-graph reactivity, no conditional branching, no fan-out beyond intra-pipeline parallelism, no cross-pipeline triggers, no first-class backfills. This spec ships exactly what's needed for scheduled dbt + dlt + sql execution.

## Out of scope

- The concrete `dlt`, `dbt`, `sql` step type implementations (lives in spec 08; this spec ships only the abstract `StepExecutor` interface and the framework that calls into it)
- The pipeline TOML schema for `pipelines/<name>.toml`, the definition reconciler, the `[seed_schedule]` *seed* applied at first registration, and `carve schedule reseed` (all live in spec 08). This spec owns the *live* `schedules` table, the scheduler that reads it, and the `carve schedule list/show/pause/resume/set-cron` mutation surface.
- REST/MCP endpoints for runtime operations (lives in spec 09; the schedules router wraps this spec's `schedules` repository)
- The static HTML UI's run-history view (lives in spec 11)
- Deploy events / `deploy.*` (Wave 2, gated — left as-is per the control-plane revision; pending the Wave 2 deploy revision of spec 14)
- Multi-tenant routing, RBAC enforcement, or hosted scaling concerns (hosted product, separate workstream)

## Behavior

### State store additions

> **Updated during implementation (2026-06-26):** the runtime's *lean first slice* shipped only the three tables the queue→run→persist loop needs — `jobs`, `workers`, and `step_runs` (Alembic migration `0008_runtime_queue`). The `schedules` / `schedule_changes` / `events` / `*_archive` tables below are **DEFERRED** to later runtime slices (they belong to the scheduler / archiver / events loops, which this slice does not ship). One correction that matters: **`step_runs` is created by THIS migration**, not carried from M1 — see the corrected note at the end of this section. The full table set below remains the spec's target design; the SQL that *shipped* is the `jobs`/`workers`/`step_runs` subset (with the partial unique indexes intact and a `jobs.error_message` column added during implementation; `id` columns are app-generated `String` rather than `UUID`).

Per ARCHITECTURE §9.3, the runtime's full table set is below (the **DEFERRED** tables are fenced inline). The shipped migration `0008_runtime_queue` creates `jobs` + `workers` + `step_runs`:

```sql
-- Active job queue  [SHIPPED — migration 0008]
CREATE TABLE jobs (
  id UUID PRIMARY KEY,
  pipeline TEXT NOT NULL,
  target TEXT NOT NULL,
  status TEXT NOT NULL,             -- queued | claimed | running | succeeded | failed | cancelled | timed_out
  trigger TEXT NOT NULL,            -- scheduled | manual | api | mcp
  scheduled_for TIMESTAMPTZ,
  claimed_by TEXT,
  claimed_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  run_id UUID,                      -- FK to runs once worker creates one
  tenant_id BIGINT NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ix_jobs_one_queued_per_pipeline
  ON jobs(pipeline, tenant_id) WHERE status = 'queued';
CREATE UNIQUE INDEX ix_jobs_one_running_per_pipeline
  ON jobs(pipeline, tenant_id) WHERE status = 'running';
CREATE INDEX ix_jobs_status_created_at
  ON jobs(status, created_at) WHERE status IN ('queued', 'claimed');
CREATE INDEX ix_jobs_heartbeat_at
  ON jobs(heartbeat_at) WHERE status IN ('claimed', 'running');

-- Archive: same schema, no partial unique indexes (historical data; dedup invariants no longer enforced)  [DEFERRED — archiver slice]
CREATE TABLE jobs_archive (LIKE jobs INCLUDING ALL EXCLUDING INDEXES);
CREATE INDEX ix_jobs_archive_pipeline_finished_at ON jobs_archive(pipeline, finished_at DESC);

-- Worker registration  [SHIPPED — migration 0008]
CREATE TABLE workers (
  id TEXT PRIMARY KEY,              -- "<hostname>:<pid>:<startup-uuid>"
  host TEXT NOT NULL,
  pid INTEGER NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL,             -- as shipped: active | stopped (the starting/draining states are a later-slice nicety)
  label TEXT,                       -- added during implementation: the worker-placement label seam (carve worker --label); nullable
  tenant_id BIGINT NOT NULL DEFAULT 1
);

-- Per-step persistence the real StepSink writes  [SHIPPED — migration 0008; CREATED here, NOT carried from M1]
CREATE TABLE step_runs (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  step_id TEXT NOT NULL,
  step_type TEXT NOT NULL,          -- dlt | dbt | sql | ...
  status TEXT NOT NULL DEFAULT 'running',  -- running | succeeded | failed | skipped
  attempt INTEGER NOT NULL DEFAULT 1,
  outputs JSONB NOT NULL DEFAULT '{}'::jsonb,  -- named step outputs for downstream Jinja
  error_message TEXT,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  duration_ms INTEGER,
  created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_step_runs_run_id ON step_runs(run_id);
CREATE INDEX ix_step_runs_run_id_step_id_attempt ON step_runs(run_id, step_id, attempt);

-- Archive tables for runs, logs, step_runs (created here so the archiver has somewhere to write)  [DEFERRED — archiver slice]
CREATE TABLE runs_archive (LIKE runs INCLUDING ALL EXCLUDING INDEXES);
CREATE TABLE logs_archive (LIKE logs INCLUDING ALL EXCLUDING INDEXES);
CREATE TABLE step_runs_archive (LIKE step_runs INCLUDING ALL EXCLUDING INDEXES);

CREATE INDEX ix_runs_archive_pipeline_finished_at ON runs_archive(pipeline, finished_at DESC);
CREATE INDEX ix_logs_archive_run_id_timestamp ON logs_archive(run_id, timestamp);
CREATE INDEX ix_step_runs_archive_run_id ON step_runs_archive(run_id);

-- Durable event log (subscribers may include the audit log in hosted)  [DEFERRED — events slice]
CREATE TABLE events (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  payload JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  tenant_id BIGINT NOT NULL DEFAULT 1
);
CREATE INDEX ix_events_unprocessed ON events(occurred_at) WHERE processed_at IS NULL;

-- Live schedule (DATA): created + owned here. The reconciler (spec 08) seeds/maintains rows at
-- first registration; the scheduler below reads this as its source of truth; `carve schedule
-- pause/resume/set-cron` mutate it live. See ARCHITECTURE §9.1.  [DEFERRED — scheduler slice]
CREATE TABLE schedules (
  id UUID PRIMARY KEY,
  pipeline TEXT NOT NULL,
  cron TEXT NOT NULL,
  target TEXT NOT NULL,
  paused BOOLEAN NOT NULL DEFAULT false,  -- the gate: list_due skips WHERE paused
  paused_by TEXT,                         -- pause origin: user | recovery; NULL iff active
  pause_reason TEXT,                      -- human-readable reason, denormalized for `schedule list`; NULL iff active
  timezone TEXT NOT NULL DEFAULT 'UTC',
  last_fired_at TIMESTAMPTZ,
  next_fires_at TIMESTAMPTZ,
  tenant_id BIGINT NOT NULL DEFAULT 1,
  -- pause origin is set iff paused; there is no 'code' origin ([seed_schedule] cannot pause, spec 08)
  CONSTRAINT ck_schedules_pause_origin CHECK (
    (paused = false AND paused_by IS NULL) OR
    (paused = true  AND paused_by IN ('user', 'recovery'))
  )
);
CREATE UNIQUE INDEX ix_schedules_one_per_pipeline ON schedules(pipeline, tenant_id);
CREATE INDEX ix_schedules_due ON schedules(next_fires_at) WHERE paused = false;

-- Schedule change audit log (the schedule is DATA; this is its audit trail, replacing git history for schedule edits)  [DEFERRED — scheduler slice]
CREATE TABLE schedule_changes (
  id BIGSERIAL PRIMARY KEY,
  pipeline TEXT NOT NULL,
  change_kind TEXT NOT NULL,        -- pause | resume | set_cron | reseed  (a timezone change rides under set_cron --timezone)
  before JSONB,                     -- prior schedule row state (NULL on first registration)
  after JSONB,                      -- new schedule row state
  actor_token_id TEXT,              -- who made the change (token id); NULL for the code seed and for recovery auto-actions
  source TEXT NOT NULL,             -- cli | api | mcp | ui | seed | reseed | recovery
  reason TEXT,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  tenant_id BIGINT NOT NULL DEFAULT 1
);
CREATE INDEX ix_schedule_changes_pipeline_changed_at ON schedule_changes(pipeline, changed_at DESC);
```

Notes:
- `tenant_id` defaults to `1` per ARCHITECTURE §9.9 multi-tenancy readiness
- Partial unique indexes include `tenant_id` so the constraint is per-tenant in hosted
- Archive tables use `LIKE ... INCLUDING ALL EXCLUDING INDEXES` to inherit columns + types but specify their own indexes (different access patterns)
- > **Corrected during implementation (2026-06-26):** the original note here claimed `runs`, `logs`, **and `step_runs`** "were added in earlier specs (01 carries them forward from M1)… this spec only adds their archives." That was **false for `step_runs`**: against the tree, `core/state/models.py` carried `Run` + `Log` from M1 but had **no `StepRun` model and no `step_runs` table**. The persisting `StepSink` needs it, so **this capability CREATES `step_runs`** in migration `0008_runtime_queue` (DDL above). Corrected: `runs` and `logs` are carried forward from M1; **`step_runs` is created by this spec's migration 0008** (its `*_archive` siblings stay deferred to the archiver slice). 08 may extend `runs`/`logs` with cols.
- The `schedules` table (`id PK, pipeline FK UNIQUE, cron, target, paused, paused_by, pause_reason, timezone, last_fired_at, next_fires_at` — ARCHITECTURE §9.1) **is created here** (migration 0008, above), resolving the prior ownership gap where neither spec created it. The reconciler ([pipelines](./pipelines.md)) **seeds/maintains the row** at first registration, applying the pipeline's optional `[seed_schedule]` block as the **initial seed** (cron/timezone/target only — it cannot seed `paused`). Thereafter the row is **live data**: this spec's scheduler reads it as the source of truth, and `carve schedule pause/resume/set-cron` (CLI/API/UI) mutate it instantly. `paused` is the boolean gate `list_due` skips on; `paused_by ∈ {user, recovery}` (NULL iff active) records the **pause origin**, which gates recovery auto-resume (see *Schedule mutations* below). The reconciler runs at `carve serve` boot — well after this migration — so the table always exists before the first row is seeded.

### Scheduler

`src/carve/runtime/scheduler.py` implements a single asyncio loop inside `carve serve`:

```python
async def scheduler_loop(state_store: StateStore, *, interval_s: float = 30.0, clock: Clock = system_clock):
    while not shutdown_requested:
        due = await state_store.schedules.list_due(now=clock.now())
        for schedule in due:
            try:
                await job_queue.enqueue_scheduled(
                    pipeline=schedule.pipeline,
                    target=schedule.target,
                    scheduled_for=schedule.this_tick_at(clock.now()),
                )
                await state_store.schedules.set_last_fired(schedule.id, clock.now())
            except QueuedJobAlreadyExists:
                # Partial unique index conflict; emit schedule.skipped and continue
                await events.emit("schedule.skipped", {
                    "pipeline": schedule.pipeline,
                    "scheduled_for": schedule.this_tick_at(clock.now()).isoformat(),
                    "reason": "queued_job_already_exists",
                })
        await sleep_until_next_tick(clock, interval_s)
```

Key properties:
- Runs as a single task per `carve serve` process; hosted uses leader election (out of scope for this spec)
- Cron evaluation via `croniter` (already pinned in M1); each schedule's `this_tick_at(now)` returns the canonical cron-tick timestamp for the current window. `list_due` evaluates cron against `now`; after enqueuing a fire, `set_last_fired` records `last_fired_at` **and recomputes `next_fires_at`** to the following tick, so the `ix_schedules_due` partial index (built on `next_fires_at`) stays accurate rather than going stale after the first fire
- Sleeps until the next 30-second wall-clock boundary, not `now + 30s` — keeps fires aligned with cron expressions like `*/5 * * * *`
- A `Clock` abstraction makes the loop deterministic in tests (no `time.sleep`)
- **The scheduler reads the `schedules` table as the single source of truth.** It does not read `pipelines/<name>.toml`, the reconciler, or any `[seed_schedule]` block — the live row (cron, timezone, `paused`) is authoritative. A paused row (`paused = true`) is skipped by `list_due`. This is the data tier of the three-tier code/data split ([../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md)): definition is code (reconciled by spec 08), schedule is data (this table), run state is data.

### Schedule mutations (live data)

The schedule is **data**, mutated instantly via `carve schedule` (and the equivalent REST/MCP surface wired in spec 09). `src/carve/cli/schedule.py` ships:

```
carve schedule list                         # all schedules with cron, timezone, paused, last/next fire
carve schedule show <pipeline>
carve schedule pause <pipeline> [--reason]
carve schedule resume <pipeline> [--reason]
carve schedule set-cron <pipeline> "<cron>" [--timezone TZ] [--reason]
```

These call `state_store.schedules` mutators (`pause`, `resume`, `set_cron`), each of which, in one transaction:
1. Updates the `schedules` row (recomputes `next_fires_at` via `croniter` on a cron/timezone change).
2. Appends a `schedule_changes` row capturing `before`/`after`, `actor_token_id`, `source`, and optional `reason`.
3. Emits the matching `schedule.*` event.

A user-initiated `pause` sets `paused = true, paused_by = 'user'` (and `pause_reason` from `--reason`); `resume` clears all three back to active. `source` records the interface (`cli`/`api`/`mcp`/`ui`).

The change takes effect on the **next scheduler tick** (≤ the 30s loop interval) — no deploy, no reconcile, no PR. RBAC is enforced via the `schedule` scope (hosted; the OSS single-token install is unscoped). This is the audited, instant path the control-plane model specifies; `carve schedule reseed` (spec 08) is the separate code→data re-seed for when a team deliberately wants to re-apply `[seed_schedule]` (it writes a `schedule_changes` row with `source = "reseed"`).

#### Pause origin and recovery auto-pause/auto-resume

The runtime also exposes two **system mutators** the recovery flow uses (spec 17); both write `schedule_changes` with `source = 'recovery'`, `actor_token_id = NULL`:

- **`auto_pause_recovery(pipeline, reason)`** — fired when a run's retries are exhausted (the `run.failed` → auto-pause trigger this spec owns). Transitions **active → `paused, paused_by = 'recovery'`** only. If the schedule is **already paused by a user**, it is **left untouched** — recovery never overrides or relabels a human's pause; it still records the diagnosis and notifies.
- **`auto_resume_recovery(pipeline)`** — fired when the resolving deploy lands (carrying the `investigation_id`, spec 14/17). Resumes **only if the row is still `paused_by = 'recovery'`**. If a user paused it in the interim (`paused_by = 'user'`), auto-resume is **suppressed** — the Investigation still transitions to `resolved`, but the schedule stays paused until a human resumes it.

This origin gate is the entire residue of UC2's retired "precedence" concern, reduced to one column and two rules: **a human's explicit pause always wins over the recovery engine's automatic one.** There is no `paused_by = 'code'` — `[seed_schedule]` cannot pause (spec 08), so every pause is either `user` or `recovery`.

> **Supersedes UC2's TTL-precedence machinery.** UC2 previously routed schedule changes through plan/build/deploy/PR, with runtime *overrides* that survived reconciles until a TTL (`schedule override` / `clear-override`, `member_override_max_ttl`). Under the control-plane model the schedule is just data, so there is no code-vs-override conflict to arbitrate and **no TTL, no override-survival logic, no `member_override_max_ttl`** — `carve schedule pause/resume/set-cron` *are* the change, audited via `schedule_changes`. See [Design notes](#design-notes).

### Job queue

> **Updated during implementation (2026-06-26):** the job queue **shipped** in the lean first slice, at **`src/carve/core/state/job_queue.py`** (it lives next to the state store's `repository.py` and shares its `sessionmaker`, rather than under `runtime/`). The state store is **synchronous** SQLAlchemy, so the queue methods are plain **sync** (`def`, not `async def`); the async `execute_pipeline`/`StepSink` call them off the event loop via `asyncio.to_thread`. The conceptual `async def` signatures below are accurate as *intent*; the shipped signatures are sync, take `pipeline`/`target` then keyword-only `scheduled_for`/`tenant_id`, and `enqueue_scheduled`'s `scheduled_for` is optional (defaults `None`). Job/run/worker ids are app-generated `String`s (e.g. `job_<uuid hex>`), not DB `UUID`s. The class is `JobQueue`, exposing the methods below **plus**: `update_heartbeat` (stamps `heartbeat_at`; the *loop* is deferred), `get_job`/`get_worker`/`list_step_runs` accessors, `register_worker`/`unregister_worker`, and `create_step_run`/`finish_step_run` (the persisting-sink seam). **SHIPPED** this slice: `enqueue_scheduled`, `enqueue_manual`, `claim_next`, `transition_to_running`, `mark_finished`, `release_claim`. The scheduler that would *call* `enqueue_scheduled` is **DEFERRED** (no scheduler loop ships this slice).

`JobQueue` (conceptually) exposes:

```python
async def enqueue_scheduled(
    pipeline: str, target: str, scheduled_for: datetime, tenant_id: int = 1
) -> Job:
    """Insert with ON CONFLICT (partial unique index) DO NOTHING.
    Raises QueuedJobAlreadyExists if a queued job for this pipeline exists.
    Returns the inserted Job."""

async def enqueue_manual(
    pipeline: str, target: str, trigger: str, tenant_id: int = 1
) -> Job:
    """Insert. On conflict, UPDATE the existing queued job:
    - trigger='manual'
    - scheduled_for=NULL
    Returns the existing (now-updated) or newly-created Job."""

async def claim_next(worker_id: str, tenant_id: int = 1) -> Optional[Job]:
    """FOR UPDATE SKIP LOCKED claim against the oldest queued job.
    Returns None if nothing queued."""

async def transition_to_running(job_id: UUID, run_id: UUID) -> None:
    """claimed → running. Sets run_id. Per-pipeline serialization check happens here:
    if another job for the same pipeline is already 'running', this raises
    PipelineAlreadyRunning and the caller releases the claim back to 'queued'."""

async def mark_finished(job_id: UUID, status: str, error_message: Optional[str] = None) -> None:
    """running → succeeded | failed | cancelled | timed_out."""

async def release_claim(job_id: UUID) -> None:
    """claimed → queued (used by the serialization fallback)."""
```

The `enqueue_manual` semantics implement the manual-trigger dedup described in [ARCHITECTURE §4.3](../ARCHITECTURE.md): 50 manual requests in rapid succession produce 1 running + 1 queued, with the 2nd–50th returning the same job_id as the 2nd's resulting upsert.

### Optimistic claim SQL

`claim_next` uses the SQL from ARCHITECTURE §4.3:

```sql
UPDATE jobs
SET status='claimed',
    claimed_by=$worker_id,
    claimed_at=now(),
    heartbeat_at=now()
WHERE id = (
  SELECT id FROM jobs
  WHERE status='queued' AND tenant_id=$tenant_id
  ORDER BY scheduled_for ASC NULLS LAST, created_at ASC
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

`FOR UPDATE SKIP LOCKED` is the critical Postgres feature: it lets concurrent workers race without blocking. Each queued job is claimed by exactly one worker; losers see no row matched and sleep before retrying.

> **Updated during implementation (2026-06-26):** the shipped `claim_next` adds one predicate to the inner `SELECT` — `AND (scheduled_for IS NULL OR scheduled_for <= now)` — so a future-dated scheduled job isn't claimed before its time (a manual job, with `scheduled_for IS NULL`, is always due). The ordering and `FOR UPDATE SKIP LOCKED` are otherwise the spec's exact query.

### Worker loop

> **Updated during implementation (2026-06-26):** a **minimal** worker shipped at `src/carve/runtime/worker.py` — the smallest thing that closes claim → run → persist. The shape differs from the sketch below: a `run_once(ctx)` coroutine claims and runs **at most one** job (returning whether one ran), and `worker_loop(ctx, …)` polls `run_once` on an interval until a `shutdown` event is set, registering a `workers` row on entry and unregistering on exit. State flows through a `WorkerContext` dataclass (the sync `Repository` + `JobQueue`, `ProjectPaths`, connections, dbt executable, and an injectable `registry_factory` for creds-free tests). It builds the run via `execute_pipeline(..., sink=PersistingStepSink(run_id, job_queue))` over a freshly built `dlt→dbt→sql` registry (`build_step_executor_registry`). **The load-bearing safety property:** once a job is claimed it is the worker's, so **any** failure after the claim — a setup DB error (create-run / transition / status write) just as much as an execute error — marks the job **and** run `failed` (best-effort), so a claimed job is never orphaned. This matters precisely because **the reaper that would otherwise reclaim a stuck job is DEFERRED** this slice. `PipelineAlreadyRunning` is the one non-failure exit: the claim is released and the run cancelled. **DEFERRED:** the **heartbeat *loop*** (the `heartbeat_at` column ships and is stamped once at claim, but no `HeartbeatHandle`/background beat), the **reaper**, **crash recovery**, and the worker-pool fan-out (`--workers N` > 1 is rejected). `execute_pipeline` is the already-shipped pipelines entry point (Increment 3), not a spec-08 future. The original async sketch below is retained as design intent.

`src/carve/runtime/worker.py` (original design sketch):

```python
async def worker_loop(worker_id: str, state_store: StateStore, *, poll_interval_s: float = 2.0):
    await state_store.workers.register(worker_id)
    try:
        while not shutdown_requested:
            job = await job_queue.claim_next(worker_id)
            if job is None:
                await sleep(poll_interval_s)
                continue
            try:
                run = await state_store.runs.create_for_job(job)
                await job_queue.transition_to_running(job.id, run.id)
                heartbeat = await heartbeat.start(job.id)
                try:
                    result = await execute_pipeline(run)
                    await job_queue.mark_finished(job.id, status=result.status, error_message=result.error)
                finally:
                    await heartbeat.stop()
            except PipelineAlreadyRunning:
                # Per-pipeline serialization fallback
                await job_queue.release_claim(job.id)
            except Exception as e:
                # Worker-side exception (not a step failure); mark failed and log
                await job_queue.mark_finished(job.id, status="failed", error_message=str(e))
                logger.exception("worker exception during job execution", job_id=job.id)
    finally:
        await state_store.workers.unregister(worker_id)
```

`execute_pipeline` is the entry point spec 08 will define — it walks the pipeline's step DAG, invoking step executors per type. This spec doesn't ship `execute_pipeline`; spec 08 does.

### Worker pool (`carve serve --workers N`)

`src/carve/runtime/worker_pool.py` spawns N `worker_loop` tasks as asyncio coroutines within a single Python process. All share the same event loop, same DB connection pool, same registered worker_ids (each gets a unique id with a `:taskN` suffix).

For users who want cross-machine scaling: separate `carve worker` processes (next section) coordinate via the same Postgres queue. The architecture is identical whether workers live in one process or many — the queue is the only coordination point.

### Worker placement & labeling

By default any worker claims any job (the flat `FOR UPDATE SKIP LOCKED` pool). But some steps must run **in a specific place** — e.g. a `dbt` step whose [execution backend](./dbt-execution.md) is `local` and must run on the team's own dbt server (its VPC reach + pinned dbt env), or a `dlt` step that must run next to a locked-down source. For these, a worker **advertises labels** (`carve worker --label onprem-dbt`) and a component/step can **require** one (`worker_label = "onprem-dbt"`). The claim query filters by label, so a labeled job is only picked up by a matching worker; unlabeled jobs run anywhere. This is the standard self-hosted-runner pattern (GitHub runner labels, k8s node-selectors) — it's how "run dbt on our own server" (the co-located-worker case) and any near-the-data execution is expressed, and it's **general** (serves `dlt`/`sql` too), not dbt-specific.

### Heartbeats

`src/carve/runtime/heartbeat.py`:

```python
async def start(job_id: UUID, *, interval_s: float = 10.0) -> HeartbeatHandle:
    handle = HeartbeatHandle(job_id, interval_s)
    handle.task = asyncio.create_task(_heartbeat_loop(handle))
    return handle

async def _heartbeat_loop(handle: HeartbeatHandle):
    while not handle.cancelled:
        try:
            await job_queue.update_heartbeat(handle.job_id)
        except Exception:
            logger.warning("heartbeat failed", job_id=handle.job_id, exc_info=True)
        await sleep(handle.interval_s)
```

Heartbeats are best-effort: a transient DB failure doesn't kill the worker; it just means a missed beat. The reaper's threshold (60s) is set to tolerate ~5 consecutive missed beats before reclaiming.

### Reaper

`src/carve/runtime/reaper.py` runs alongside the scheduler:

```python
async def reaper_loop(state_store: StateStore, *, interval_s: float = 30.0, stale_threshold_s: float = 60.0):
    while not shutdown_requested:
        reclaimed = await state_store.execute("""
            UPDATE jobs
            SET status='queued', claimed_by=NULL, claimed_at=NULL, heartbeat_at=NULL
            WHERE status IN ('claimed', 'running')
              AND heartbeat_at < now() - INTERVAL ':stale_threshold seconds'
            RETURNING id, claimed_by
        """, stale_threshold=stale_threshold_s)
        for job_id, prior_claimed_by in reclaimed:
            await events.emit("job.reclaimed", {
                "job_id": str(job_id),
                "prior_claimed_by": prior_claimed_by,
                "reason": "stale_heartbeat",
            })
        await sleep(interval_s)
```

Reclaimed jobs:
- Have any in-flight Run row marked `status='failed'` with `error_message='worker_crashed_or_unreachable'`
- Are re-claimable by the next available worker
- Their step-level state is discarded (the next worker runs the pipeline from scratch)

### Archiver

`src/carve/runtime/archiver.py` runs on its own hourly loop:

```python
async def archiver_loop(state_store: StateStore, config: ArchiveConfig):
    while not shutdown_requested:
        for table_name, window in config.windows.items():
            try:
                count = await archive_table_safely(table_name, window)
                await events.emit("archive.batch_completed", {"table": table_name, "rows_moved": count})
            except Exception:
                logger.exception("archive batch failed", table=table_name)
        await sleep(config.interval_s)

async def archive_table_safely(table_name: str, window: timedelta) -> int:
    """Move rows older than `window` from `<table>` to `<table>_archive`.
    Verify count match, then delete. Transactional."""
    async with state_store.tx() as tx:
        # Step 1: select rows to archive
        rows = await tx.fetch(f"""
            SELECT * FROM {table_name}
            WHERE finished_at < now() - $window
              AND status IN ('succeeded', 'failed', 'cancelled', 'timed_out')
        """, window=window)
        if not rows:
            return 0

        # Step 2: insert into archive table
        await tx.executemany(f"INSERT INTO {table_name}_archive VALUES ($1, $2, ...)", rows)

        # Step 3: verify count match
        archived_count = await tx.fetchval(f"""
            SELECT COUNT(*) FROM {table_name}_archive
            WHERE id IN (...)
        """)
        if archived_count != len(rows):
            raise ArchiveVerificationFailed(f"Expected {len(rows)} rows in archive, got {archived_count}")

        # Step 4: delete from active table
        await tx.execute(f"DELETE FROM {table_name} WHERE id IN (...)")

    return len(rows)
```

Archive windows per `runtime.toml`:

```toml
[runtime.archive]
interval_s = 3600                     # hourly
jobs_window = "7d"
runs_window = "30d"
logs_window = "30d"
steps_window = "30d"
```

The archiver runs alongside scheduler + reaper + worker pool in `carve serve`. In hosted, it can run as a separate process to avoid contention.

### Persisting StepSink (the seam pipelines forward-declared)

> **Added during implementation (2026-06-26):** the **real persisting `StepSink`** shipped at `src/carve/runtime/persisting_step_sink.py` (`PersistingStepSink`). Since Increment 3, `execute_pipeline` has carried a forward-declared `StepSink` Protocol defaulting to a **no-op** so the DAG walk stayed runtime-independent — **this slice fills that seam**, and it is the **first time `execute_pipeline` persists anything**. On `step_started` it inserts a `running` `step_runs` row (via `JobQueue.create_step_run`); on `step_finished` it transitions that row to the step's terminal status with the threaded `outputs` (JSONB) / `error_message` / timings (via `finish_step_run`). It threads the `step_runs.id` between start and finish per `(step_id, attempt)`. Because the state store is sync, each DB call is bridged off the event loop via `asyncio.to_thread`. The paired `step.*` **event emit stays a no-op seam** (marked `TODO(events slice)`) — the `events` table + emitter are a later slice.
>
> **Forward note (security reviewer, non-blocking — for the events/UI slice, not a change here):** the persisting sink writes step `error_message` and `outputs` **verbatim**. The later slice that first *surfaces* `step_runs` to users (events / UI) should add redaction at the surfacing boundary, since step outputs/errors can carry secrets. Recorded here so the future slice owns it.

### Step executor framework

`src/carve/runtime/step_executor_base.py`:

```python
class StepExecutor(Protocol):
    """One implementation per step type. Concrete types in spec 08."""

    step_type: ClassVar[str]                # "dlt" | "dbt" | "sql" | ...

    async def execute(self, *, step: PipelineStep, run: Run, paths: ProjectPaths) -> StepResult: ...
    async def cancel(self, *, step_run_id: UUID) -> None: ...

@dataclass
class StepResult:
    status: Literal["succeeded", "failed", "skipped"]
    outputs: dict[str, Any]                 # named outputs for downstream Jinja templating
    log_lines: list[LogLine]                # captured stdout/stderr
    error_message: Optional[str]
    duration_ms: int

class StepExecutorRegistry:
    def register(self, executor: StepExecutor) -> None: ...
    def get(self, step_type: str) -> StepExecutor: ...
```

The registry is populated at `carve serve` startup with the built-in executors (registered by spec 08). Adding a fourth step type later means registering one more executor — the framework is unchanged.

### `carve serve` lifecycle

`src/carve/cli/serve.py`:

```
carve serve [OPTIONS]

OPTIONS:
  --port INTEGER           HTTP port (default: 8765)
  --host TEXT              Host to bind (default: 127.0.0.1; warns on 0.0.0.0)
  --workers INTEGER        In-process worker count (default: 1)
  --no-scheduler           Skip the scheduler loop (useful for worker-only nodes)
  --no-reaper              Skip the reaper loop
  --no-archiver            Skip the archiver loop
  --no-auto-migrate        Don't run `alembic upgrade head` on startup
```

Startup sequence:

1. Connect to Postgres; if connection fails, retry with exponential backoff (1s, 2s, 4s, 8s, 16s, 30s — then fail with friendly error pointing at `DATABASE_URL`)
2. Run `alembic upgrade head` (unless `--no-auto-migrate`)
3. If `.carve/token` exists but no row in the `tokens` table: bootstrap it (per spec 05's deferred-bootstrap path)
4. Start the FastAPI app (spec 09); it begins accepting requests immediately
5. Start the scheduler, reaper, archiver, worker pool as asyncio tasks
6. Print "Carve is serving at http://127.0.0.1:8765 with N workers"

Graceful shutdown (SIGTERM or Ctrl-C):

1. Stop accepting new HTTP requests
2. Set `shutdown_requested = True`
3. Wait for in-flight jobs to complete (with a configurable grace period, default 5 minutes)
4. After grace period, jobs still in-flight: their heartbeats stop, the reaper will reclaim them on the next replica
5. Unregister all workers from the `workers` table
6. Close DB connection pool
7. Exit

A second SIGTERM during shutdown skips the grace period and exits immediately, leaving in-flight jobs for the reaper.

### `carve worker` lifecycle

> **Updated during implementation (2026-06-26):** `carve worker` **shipped** in the lean first slice at **`src/carve/cli/commands/worker.py`** (under `cli/commands/`, mirroring the other commands). Flags as shipped:
> ```
> carve worker [--once] [--poll-interval SECONDS] [--workers INTEGER]
> ```
> `--once` claims and runs a **single** queued job then exits (a no-op message on an empty queue); without it, the command loops `run_once` every `--poll-interval` seconds (default 1.0) until Ctrl-C. `--workers` defaults to 1 and **any value > 1 is rejected** with a clear "single worker in this slice" message — the worker-pool fan-out is **DEFERRED**. The command mirrors `carve runs`' setup (load `Config`, build the engine, `initialize_database`, construct `Repository` + `JobQueue`), resolves the active target + `ProjectPaths`/connections, and drives `worker_loop`/`run_once` over the creds-free `dlt→dbt→sql` registry. `carve serve` stays the existing stub (its FastAPI server + scheduler/reaper/archiver supervisor is **DEFERRED**).

`src/carve/cli/worker.py` (original design sketch):

```
carve worker [OPTIONS]

OPTIONS:
  --workers INTEGER        In-process worker count for this process (default: 1)
```

Same shape as `carve serve` minus the FastAPI server and scheduler/reaper/archiver. A pure worker process; suitable for scale-out deployments where one node runs the API + scheduler and other nodes run pools of workers.

### Events

Every state transition emits an event into the `events` table via `src/carve/runtime/events.py`:

| Event                      | Payload includes                                                  |
|----------------------------|-------------------------------------------------------------------|
| `schedule.skipped`         | pipeline, scheduled_for, reason                                   |
| `schedule.paused` / `resumed` | pipeline, actor_token_id, source, reason                       |
| `schedule.changed`         | pipeline, before (cron/tz), after (cron/tz), actor_token_id, source, reason |
| `schedule.reseeded`        | pipeline, before, after, source="reseed" (emitted by `carve schedule reseed`, spec 08) |
| `job.queued`               | job_id, pipeline, target, trigger, scheduled_for                  |
| `job.claimed`              | job_id, worker_id                                                 |
| `job.reclaimed`            | job_id, prior_claimed_by, reason                                  |
| `run.started`              | run_id, job_id, pipeline                                          |
| `run.succeeded` / `failed` | run_id, duration_ms, error_message                                |
| `step.started/completed/failed` | step_run_id, run_id, type, outputs (on success), error_message |
| `archive.batch_completed`  | table, rows_moved                                                 |
| `worker.registered` / `unregistered` | worker_id, host, pid                                    |

Events are durable (Postgres row) and the basis for webhooks (spec 09 wires that up).

## Tests

> **Updated during implementation (2026-06-26):** the **lean first slice** shipped tests for the queue / worker / persisting sink it delivered; the scheduler / reaper / archiver / serve / schedule-mutation bullets below are **DEFERRED** with their slices. Shipped this slice:
> - **Unit (enqueue dedup):** `enqueue_scheduled` queues a job; a second for the same pipeline raises `QueuedJobAlreadyExists`; different pipelines both queue; **concurrent** `enqueue_scheduled` yields exactly one queued job (`tests/runtime/state/test_job_queue_enqueue.py`).
> - **Unit (manual upsert):** `enqueue_manual` upserts onto the existing queued row and returns its id; inserts when none exists (same file).
> - **Unit (optimistic claim):** concurrent `claim_next` claims a job exactly once; empty queue returns `None`; a future-`scheduled_for` job is skipped; `transition_to_running` raises `PipelineAlreadyRunning` on a second running job; `release_claim` returns a claimed job to queued (`tests/runtime/state/test_job_queue_claim.py`).
> - **Unit (persisting sink):** `step_started`→`step_finished` persists a succeeded `step_run`; failure records `error_message`; retries record one `step_run` per attempt (`tests/runtime/test_persisting_step_sink.py`).
> - **Integration (worker end-to-end):** a worker runs a queued job end-to-end (job + run + `step_runs` all terminal); `run_once` on an empty queue is a no-op; a second `run_once` after completion claims nothing; **a setup failure on a claimed job marks it failed, not orphaned** (`tests/runtime/test_worker_end_to_end.py`).
> - **CLI:** `carve worker --once` exits zero on an empty queue and after running a job; `--workers > 1` is rejected (`tests/cli/commands/test_worker_command.py`).
>
> The bullets below remain the spec's full-runtime test target (**DEFERRED** with the scheduler / reaper / archiver / serve slices):

- **Unit (scheduler):** cron `*/5 * * * *` fires at expected times under a controlled `Clock`; missed ticks (clock jumps forward by 20 minutes) produce one fire, not four
- **Unit (schedule source of truth):** the scheduler fires from the `schedules` table row, not from `pipelines/<name>.toml`; a paused row is skipped by `list_due`; mutating a `[seed_schedule]` block (without `carve schedule reseed`) does not change which ticks fire
- **Integration (schedule mutation audited):** `carve schedule pause`/`resume`/`set-cron` updates the row, takes effect within one scheduler loop interval, emits the matching `schedule.*` event, and appends a `schedule_changes` row with `before`/`after`/`actor_token_id`/`source` — no deploy/reconcile involved
- **Unit (pause origin gate):** `auto_pause_recovery` sets `paused_by='recovery'` on an active row but leaves a `paused_by='user'` row untouched; `auto_resume_recovery` resumes a `paused_by='recovery'` row but is suppressed when a user paused it in the interim; the `ck_schedules_pause_origin` CHECK rejects a paused row with NULL `paused_by` (and an active row with a non-NULL one)
- **Unit (job_queue dedup):** two consecutive `enqueue_scheduled` for the same pipeline+scheduled_for: the second raises `QueuedJobAlreadyExists`
- **Unit (job_queue manual upsert):** `enqueue_manual` on a pipeline with an existing queued job updates it; the returned job_id matches the existing job's id
- **Unit (optimistic claim):** spawn 10 concurrent `claim_next` calls against 1 queued job; exactly one returns a job, nine return None
- **Unit (heartbeat):** a heartbeat loop running against a controlled clock writes `heartbeat_at` every interval; cancellation stops writes promptly
- **Unit (reaper):** synthetic job with `heartbeat_at = now() - 70s` is reclaimed; `heartbeat_at = now() - 30s` is left alone
- **Unit (archiver):** 100 completed jobs older than the window are archived; row counts match; deletion succeeds; verification failure halts the batch atomically
- **Integration (serve lifecycle):** `carve serve --workers 2` starts cleanly against an empty Postgres; SIGTERM produces graceful shutdown; worker rows are removed from the table
- **Integration (worker crash recovery):** spawn `carve worker`, queue a job, `kill -9` the worker mid-execution; reaper reclaims within 90s (60s threshold + 30s loop); next worker runs the pipeline successfully
- **Integration (concurrent claims):** spawn 5 worker processes against the same Postgres, queue 50 jobs; all 50 run, none twice
- **Integration (manual trigger dedup):** queue 50 manual triggers in rapid succession for one pipeline; database shows 1 running + 1 queued throughout; 50 client requests return 2 distinct job_ids (one for each row, with the 2nd–50th all returning the queued one)
- **Integration (scheduled while queued):** scheduler fires while a queued job exists for the same pipeline; emits `schedule.skipped`; does not insert a duplicate
- **Integration (long-running job + reaper):** a job whose execution legitimately exceeds 60s but maintains heartbeats is not reclaimed; verifies that the reaper's threshold isn't too aggressive
- **Integration (archiver verify-then-delete):** inject a synthetic failure between insert and delete; archive table has the rows, active table still has them, no data loss

## Acceptance

> **Updated during implementation (2026-06-26):** the criteria below are the **full-runtime** acceptance target and remain the spec's bar. The **lean first slice** satisfies the slice-scoped subset: **`carve worker --once`** (and the loop) against a freshly-initialized Postgres claims a queued job, creates its `runs` row, transitions it to `running`, executes the pipeline, and persists `step_runs` + the terminal `runs`/`jobs` rows; the **partial unique indexes structurally prevent more than one queued and one running job per pipeline**; **50 concurrent manual triggers** produce 1 running + 1 queued (the `enqueue_manual` upsert), not 50 queued; a worker failure after claim never orphans the job (it is marked `failed`). The scheduler / `carve schedule` / reaper / archiver / `carve serve` / graceful-shutdown criteria are **DEFERRED** with their slices.

- `carve serve --workers 1` against a freshly-initialized Postgres runs end-to-end: scheduler fires due jobs, worker claims and runs them, reaper reclaims stale claims, archiver moves old rows
- The scheduler treats the `schedules` table as the source of truth; `carve schedule pause/resume/set-cron` changes firing within one loop interval without a deploy or reconcile, and every such change appends a `schedule_changes` audit row
- A human's explicit pause always wins over recovery's automatic one: recovery's `auto_pause_recovery`/`auto_resume_recovery` never override or auto-resume a `paused_by='user'` schedule
- Per ARCHITECTURE §13.1 budgets:
  - Scheduler latency: jobs fire within 30 seconds of their cron time
  - Run startup overhead: under 10 seconds from claim to first step execution
  - Reaper detects stale workers within 60 seconds of last heartbeat
- The partial unique indexes structurally prevent more than one queued and one running job per pipeline
- A crashed worker (`kill -9`) doesn't lose data; the reaper reclaims its job and the next worker runs from scratch
- 50 concurrent manual triggers produce 1 running + 1 queued, not 50 queued
- The archiver never deletes from active tables until the archive insert has been verified
- The step executor registry accepts the three spec-08 implementations (`dlt`, `dbt`, `sql`) without modification to this spec's framework
- `carve serve` graceful shutdown completes within the configured grace period or escalates cleanly
- Full integration test for the queue dedup + crash recovery scenario passes deterministically (no flakes from timing)

## Design notes

- **Why is the schedule data, not code — and why does this supersede UC2?** The control-plane model ([../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md), "Three-tier code/data ownership") splits ownership by concern: the pipeline *definition* (steps, DAG, component refs, pins) is code reconciled into state by spec 08; the *schedule* is data living in the `schedules` table; *run state* is data. So this spec's scheduler reads the `schedules` row as the source of truth, and operators change it instantly via `carve schedule` (CLI/API/UI), audited by the `schedule_changes` log + the `schedule` RBAC scope. This **reverses UC2's earlier resolution** that schedule changes go through plan/build/deploy/PR with kubectl-style runtime *overrides* that survived reconciles until a TTL. Because the schedule is now plain data, there is no code-vs-override precedence to arbitrate: the **TTL-precedence machinery is deleted** (no `schedule override`/`clear-override`, no Option-B survival logic, no `member_override_max_ttl`), and the reconciler never touches the schedule. The tradeoff (accepted in the ADR): schedules reconstitute from the backed-up state store + the code `[seed_schedule]`, not from `git clone`.
- **Why does the reconciler not own the schedule?** Keeping the reconciler scoped to the definition is what makes graduation (simple → multi) and instant ops changes coexist: a deploy or reconcile can update steps/DAG/pins without ever clobbering a live cadence change an on-call engineer just made. The seed-once-then-data rule (`[seed_schedule]` applied only at first registration; `carve schedule reseed` to deliberately re-apply) is the single bridge between the two tiers.
- **Why a single Postgres-backed queue instead of a dedicated job queue system (Celery, RQ, Temporal, Dramatiq)?** Three reasons. (1) Carve already requires Postgres; adding Redis or Temporal expands the operational footprint significantly. (2) The optimistic-claim pattern in Postgres handles the target scale comfortably (~100s of jobs per minute is well within Postgres's reach). (3) Job-queue systems are general-purpose and bring complexity Carve doesn't need (priorities, delays, dead-letter queues, fan-out). Per design decision [5.6 narrow runtime](../ARCHITECTURE.md), we keep it small.
- **Why partial unique indexes for the dedup invariant?** Because enforcing "at most one queued / one running per pipeline" at the schema level means application code can't break the invariant by accident. The alternative (application-level locking or check-then-insert) has race conditions under concurrent inserts. Postgres's partial unique indexes are exactly the right tool here.
- **Why is the per-pipeline serialization (`PipelineAlreadyRunning`) checked at `transition_to_running` rather than at `claim_next`?** Because claim_next runs against the `queued` partial unique index, which doesn't know about running jobs. The transition to running is when we need to re-check. The cost is one extra round trip per job; the benefit is that the check is unambiguous (one query, one constraint).
- **Why does the archiver run in `carve serve` rather than as a separate process?** In OSS, simplicity. One process to monitor. In hosted, the archiver moves to a separate node when contention with the worker pool starts to matter; this is a configuration change (`--no-archiver` on `carve serve`, plus a separate `carve archive` process — out of scope for this spec but the architecture supports it).
- **Why durable events in Postgres rather than an in-memory pub/sub?** Because webhooks (spec 09) need to deliver events reliably across worker restarts. Events also become the audit log in hosted. In-memory pub/sub is the right choice for ephemeral subscribers (the static UI's live updates, when added); both can coexist.
- **Why aren't step-level retries handled by the runtime?** Because retry semantics are step-specific (a dlt step's retry is different from a dbt step's retry). The `retry` failure mode in spec 08 is implemented inside the step executor, not in the runtime's job-level loop. The job-level handles only "the worker crashed; reclaim and restart" recovery.

## Open questions

- **Heartbeat interval and stale threshold values.** *Implementation default.* 10s heartbeat, 60s stale threshold. Tunable in `runtime.toml`. Default chosen so 5 consecutive missed beats trigger reclaim — tolerant enough to handle DB hiccups, aggressive enough to recover from real crashes within a minute.
- **Worker count default for `carve serve`.** *Strategy-already-resolved.* Default 1 per the initial positioning decision (single worker, serial). Users scale with `--workers N` or `carve worker` processes.
- **Archive table partitioning.** *Implementation default.* No partitioning in OSS (a single-team install's archive grows slowly enough that partitioning is unjustified complexity). Hosted partitions by month for query performance — that's a hosted-side concern, out of scope for this spec.
- **Backpressure when the queue is overloaded.** *Implementation default.* No special handling initially; if the queue grows unbounded, that's the user's signal to add workers. Future enhancement: a `max_queue_depth` setting that rejects new triggers above the threshold. Defer until someone hits it.
- **Behavior when Postgres becomes unreachable mid-run.** *Implementation default.* Workers attempt to reconnect with backoff; heartbeats stop; reaper reclaims the job after threshold. The in-flight subprocess (dlt/dbt) keeps running and may complete its destination writes — those are idempotent enough (dlt's incremental state, dbt's run_results) that the eventual rerun won't double-write. Documented in `docs/runtime-troubleshooting.md`.
- **Ownership of `schedule_changes` and the `carve schedule` live-mutation surface.** *Needs human confirmation — smallest-reasonable choice made.* The reference model and ADR name the `schedule_changes` audit log + the `schedule` RBAC scope but do not pin which spec ships them; spec 08 explicitly delegates the live `schedules` table and `carve schedule list/show/pause/resume` to this spec (08 §"Out of scope"). So this spec ships the `schedule_changes` table (migration 0008) and `cli/schedule.py`, while spec 08 retains `[seed_schedule]` + `carve schedule reseed`. Confirm this split (vs. ARCHITECTURE §9.3 listing `schedules` among earlier tables) when specs 08/09 are next touched.
