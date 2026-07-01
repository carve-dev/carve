# Runtime: scheduler, job queue, workers, heartbeats, reaper, archiver

> The largest net-new module. Ships the scheduling, queueing, worker process model, optimistic-claim semantics, crash recovery, and archive flow described in [ARCHITECTURE §4](../ARCHITECTURE.md). Implements the runtime that PRD's [§6.8 Scheduling](../PRD.md) and [§6.6 Run](../PRD.md) describe at the product level.

## Status

- **Status:** Drafting
- > **Lean first slice landed (2026-06-26).** The runtime is the largest net-new module; it ships in slices. **This first slice shipped the queue → run → persist loop**: migration `0008_runtime_queue` (`jobs` + `workers` + `step_runs`, with the two partial unique indexes); the **sync `JobQueue`** (`core/state/job_queue.py` — `enqueue_scheduled` ON-CONFLICT dedup, `enqueue_manual` upsert, `FOR UPDATE SKIP LOCKED` `claim_next`, `transition_to_running`/`mark_finished`/`release_claim`, worker register/unregister, `create_step_run`/`finish_step_run`); the **real persisting `StepSink`** (`runtime/persisting_step_sink.py` — fills the no-op seam pipelines forward-declared, so `step_runs` persist for the first time); a **minimal worker** (`runtime/worker.py` — `run_once`/`worker_loop`, claim-then-never-orphan); and **`carve worker`** (`cli/commands/worker.py`, `--once`/loop). **DEFERRED to later runtime slices** (each fenced inline below): the **scheduler loop + live `schedules` table + `carve schedule` CLI + `schedule_changes`**; the **heartbeat loop + reaper** (the `heartbeat_at` column ships and is stamped once at claim; the loops defer); the **archiver + `*_archive` tables**; the full **`carve serve`** supervisor + the **`events` table/emitter** (the `step.*` emit is a marked no-op seam); the **worker-pool fan-out** (`--workers N`); and **crash recovery**. Status stays **Drafting** — more slices remain.
- > **Increment-4 scope complete (worker-placement slice, 2026-07-01).** The **7th** runtime slice — worker placement (`jobs.required_label` + the label-filtered `claim_next` + the `resolve_required_label` reducer with author-time conflict reject + the scheduler resolver-callback + `carve worker`/`carve serve --label`) — landed, **completing runtime's Increment-4 scope** (all seven slices: queue/worker, scheduler, heartbeat+reaper, events, archiver, worker-pool, worker-placement). Gates green (**2328 pytest**, 6 skipped, 0 failed); all three reviewers PASS after 1 polish pass. The only runtime behavior still deferred in this spec is the **recovery auto-pause/auto-resume mutators** (`auto_pause_recovery`/`auto_resume_recovery`, §"Pause origin and recovery auto-pause/auto-resume"), which land with [recovery](./recovery.md) in **Increment 6** — Status therefore stays **Drafting** until they ship.
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

> **Updated during implementation (2026-06-26):** the runtime's *lean first slice* shipped only the three tables the queue→run→persist loop needs — `jobs`, `workers`, and `step_runs` (Alembic migration `0008_runtime_queue`). One correction that matters: **`step_runs` is created by THIS migration**, not carried from M1 — see the corrected note at the end of this section.
>
> **Updated during implementation (2026-06-26, scheduler slice):** the **scheduler slice** (migration `0009_runtime_schedules`) now ships the **`schedules` + `schedule_changes`** tables — un-fenced below from `[DEFERRED — scheduler slice]` to `[SHIPPED — migration 0009]`. Implementation deltas worth recording: (1) the **`ck_schedules_pause_origin` CHECK carries a `paused_by IS NOT NULL` guard** (both the ORM `__table_args__` and the migration) — without it a `paused=true, paused_by=NULL` row passes, because a SQL-NULL-valued CHECK passes in Postgres; this is the **CHECK-NULL bug fixed during this slice**, regression-tested at both the ORM and raw-SQL layers; (2) `id` columns are app-generated `String` (`sched_<uuid hex>`), not DB `UUID`; (3) `schedule_changes.actor_token_id` is **nullable pending the auth slice** (the CLI writes `source='cli'`, `actor_token_id=NULL`); (4) the per-mutation event emit is a **no-op `_emit(kind, payload)` seam** (`schedules._emit`, `TODO(events slice)`), not a live emitter — the `events` table + emitter stay deferred. The `events` / `*_archive` tables below remain **DEFERRED** to later runtime slices (the archiver / events loops, which this slice does not ship). The full table set below is the spec's target design.
>
> **Shipped during implementation (events slice, 2026-06-29):** the **events slice** (migration `0010_runtime_events`) ships the durable **`events`** table — un-fenced below from `[DEFERRED — events slice]` to `[SHIPPED — migration 0010]` (superseding the scheduler-slice note just above that the `events` table stayed deferred). Shipped shape: **`id` is `BIGSERIAL`** (`Mapped[int]` / `autoincrement`, mirroring `Log` / `ScheduleChange` — **not** the app-generated `String` ids of the entity tables); **`payload` is `JSONB NOT NULL`** (no default — the emitter always supplies the taxonomy payload); the **partial** index `ix_events_unprocessed ON events(occurred_at) WHERE processed_at IS NULL` ships (the webhook-cursor seam, like `ix_schedules_due`). With it, every previously-no-op `_emit(kind, payload)` seam (`job_queue._emit`, `schedules._emit`, the sink's `step.*`) and the worker's run-lifecycle transitions persist a durable row **when an emitter is injected** — see §Events for the mechanism. The **`*_archive` tables stay `[DEFERRED — archiver slice]`**.
>
> **Shipped during implementation (archiver slice, 2026-06-29):** the **archiver slice** (migration `0011_runtime_archive`) ships the four **`*_archive`** tables — un-fenced below from `[DEFERRED — archiver slice]` to `[SHIPPED — migration 0011]` (each a `LIKE … INCLUDING ALL EXCLUDING INDEXES` clone of its active table + its one access index; `LIKE` copies columns/defaults/CHECKs but **never FKs**, so the archives stand alone and a `runs` delete is never blocked by an archived child). Two **tree-reconciliations** worth recording: **(a)** `ix_runs_archive_pipeline_finished_at` is built on **`(pipeline_name, completed_at DESC)`** — the live `runs` table carries `pipeline_name`/`completed_at`, not the sketch's `pipeline`/`finished_at`; the documented index *name* is kept. **(b) No `*Archive` ORM models** — the archive op is the column-list-agnostic `INSERT INTO <t>_archive SELECT * FROM <t> WHERE <predicate>` + `DELETE` in raw core SQL (the `reclaim_stale` precedent — four near-duplicate models would buy nothing). With this, the State store's full target table set is now entirely shipped.

Per ARCHITECTURE §9.3, the runtime's full table set is below (the **DEFERRED** tables are fenced inline). The shipped migration `0008_runtime_queue` creates `jobs` + `workers` + `step_runs`:

```sql
-- Active job queue  [SHIPPED — migration 0008]
CREATE TABLE jobs (
  id UUID PRIMARY KEY,
  pipeline TEXT NOT NULL,
  target TEXT NOT NULL,
  status TEXT NOT NULL,             -- queued | claimed | running | succeeded | failed | cancelled | timed_out
  trigger TEXT NOT NULL,            -- scheduled | manual | api | mcp
  required_label TEXT,              -- [SHIPPED — migration 0013] worker-placement filter (NULL = any worker); a derived reduction over the pipeline's components' worker_labels. The claim filters on it (see §"Worker placement & labeling").
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

-- Archive: same schema, no partial unique indexes (historical data; dedup invariants no longer enforced)  [SHIPPED — migration 0011]
-- The 0011 LIKE-clone is a one-time copy that does NOT track later ALTERs, so migration 0013 adds
-- `required_label` to BOTH `jobs` and `jobs_archive` to keep the clone column-parallel — the parity
-- the archiver's `INSERT INTO jobs_archive SELECT * FROM jobs` relies on (cross-slice invariant).
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

-- Archive tables for runs, logs, step_runs (created here so the archiver has somewhere to write)  [SHIPPED — migration 0011]
CREATE TABLE runs_archive (LIKE runs INCLUDING ALL EXCLUDING INDEXES);
CREATE TABLE logs_archive (LIKE logs INCLUDING ALL EXCLUDING INDEXES);
CREATE TABLE step_runs_archive (LIKE step_runs INCLUDING ALL EXCLUDING INDEXES);

-- SHIPPED on (pipeline_name, completed_at DESC): the live `runs` table carries
-- `pipeline_name`/`completed_at`, NOT this sketch's `pipeline`/`finished_at`; the
-- documented index NAME is kept (tree-reconciliation, see the callout above).
CREATE INDEX ix_runs_archive_pipeline_finished_at ON runs_archive(pipeline_name, completed_at DESC);
CREATE INDEX ix_logs_archive_run_id_timestamp ON logs_archive(run_id, timestamp);
CREATE INDEX ix_step_runs_archive_run_id ON step_runs_archive(run_id);

-- Durable event log (subscribers may include the audit log in hosted)  [SHIPPED — migration 0010]
-- Shipped shape: `id` is BIGSERIAL (Mapped[int]/autoincrement, mirrors Log/ScheduleChange);
-- `payload` is JSONB NOT NULL (no default — the emitter supplies it); the partial
-- ix_events_unprocessed ON events(occurred_at) WHERE processed_at IS NULL ships (webhook cursor).
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
-- pause/resume/set-cron` mutate it live. See ARCHITECTURE §9.1.  [SHIPPED — migration 0009]
-- Shipped shape: `id` is an app-generated String (`sched_<hex>`), not UUID; the CHECK below
-- carries the load-bearing `paused_by IS NOT NULL` guard (see the callout above).
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
  -- SHIPPED with a `paused_by IS NOT NULL` guard: a bare `paused_by IN (...)` yields SQL NULL for a
  -- NULL origin, and a NULL-valued CHECK PASSES in Postgres — so the guard is what actually rejects a
  -- `paused=true, paused_by=NULL` row (the CHECK-NULL bug fixed this slice; regression-tested ORM + raw-SQL).
  CONSTRAINT ck_schedules_pause_origin CHECK (
    (paused = false AND paused_by IS NULL) OR
    (paused = true  AND paused_by IS NOT NULL AND paused_by IN ('user', 'recovery'))
  )
);
CREATE UNIQUE INDEX ix_schedules_one_per_pipeline ON schedules(pipeline, tenant_id);
CREATE INDEX ix_schedules_due ON schedules(next_fires_at) WHERE paused = false;

-- Schedule change audit log (the schedule is DATA; this is its audit trail, replacing git history for schedule edits)  [SHIPPED — migration 0009]
-- Shipped shape: `actor_token_id` is nullable pending the auth slice (the CLI writes source='cli', actor_token_id=NULL).
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

> **Updated during implementation (2026-06-26, scheduler slice):** the scheduler **shipped** at `src/carve/runtime/scheduler.py`, factored into two functions rather than the single inline loop sketched below: a synchronous **`run_due_once(schedules, job_queue, now, *, tenant_id=1) -> int`** (one deterministic pass: `list_due` → `enqueue_scheduled(scheduled_for=this_tick)` → `set_last_fired`, returning the count enqueued) and the async **`scheduler_loop(schedules, job_queue, *, interval_s=30.0, clock=system_clock, shutdown=None, tenant_id=1)`** that bridges each `run_due_once` off the event loop via **`asyncio.to_thread`** (the state store is sync — same pattern as the shipped `worker.py`) and sleeps to the next boundary. Concrete shipped shape vs. the sketch: the repo is a constructed **`Schedules`** object (`core/state/schedules.py`), not `state_store.schedules`; cron math lives in the **`runtime/cron.py`** module functions **`this_tick_at(cron, now, timezone)` / `next_tick_after(...)`** (timezone-aware via croniter + zoneinfo, DST-correct, raising typed `CronError` on an unsatisfiable expression), not a `schedule.this_tick_at()` method; the `Clock` seam is `runtime/clock.py` (`Clock` Protocol / `system_clock` / `FakeClock`) with **`sleep_until_next_boundary(interval_s)`** doing the epoch-aligned boundary math. `set_last_fired` advances `next_fires_at` to the FOLLOWING tick in the same transaction (the partial-index-stays-accurate property below holds). The `schedule.skipped`/`schedule.fired` emits go through the no-op **`schedules._emit`** seam (no `events` table this slice). A pass that raises is logged and swallowed so one bad poll never kills the loop. The default interval shipped as **30s**. The conceptual loop below is retained as design intent.
>
> **Shipped during implementation (events slice, 2026-06-29):** the scheduler's `schedule.skipped` (and `schedule.fired`) emit through `schedules._emit`, which is now **live** when `Schedules` is constructed with an injected `emitter` (`carve serve` wires it) — no call-site change, the seam flipped from no-op to durable.

`src/carve/runtime/scheduler.py` implements a single asyncio loop inside `carve serve` (original design sketch):

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

> **Updated during implementation (2026-06-26, scheduler slice):** the live-mutation surface **shipped**. The CLI lives at **`src/carve/cli/commands/schedule/`** (`__init__.py` Typer group + `commands.py`), not `cli/schedule.py`; the mutators are methods on the constructed **`Schedules`** repo (`core/state/schedules.py`: `pause`/`resume`/`set_cron`, plus `seed`/`set_last_fired`/`list_due`/`list_all`/`get`/`list_changes`). Each mutator runs **one transaction** that row-locks (`FOR UPDATE`), mutates the row, recomputes `next_fires_at` on a cron/timezone change, appends a `schedule_changes` audit row, and calls the **no-op `_emit` seam** (the `schedule.*` event-emit stays a seam — `events` table deferred). Shipped deltas vs. the sketch: `set-cron`'s target flag is **`--target-pipeline`** (not `--target`), and `set_cron` **UPSERTs** — it creates the row if absent (default target `'prod'`) so a schedule can be stood up end-to-end without the deferred reconciler-seed; cron + timezone are validated up front (exit 2). **STILL DEFERRED (kept fenced below):** the recovery **`auto_pause_recovery`/`auto_resume_recovery`** system mutators (the `paused_by='recovery'` column value + CHECK origin ship; the recovery mutator integration defers — they exist in the spec body, not in code); `carve schedule reseed` (a deferred stub that exits non-zero with a clear message — the `[seed_schedule]` re-apply is PIPELINES/spec-08's job).

> **Shipped during implementation (events slice, 2026-06-29):** each mutator's `_emit` seam (`schedule.seeded`/`paused`/`resumed`/`changed`) is now **live** when `Schedules` is constructed with an injected `emitter` (the `carve schedule` CLI wires it), persisting a durable `events` row alongside the `schedule_changes` audit row it already wrote — no call-site change. The **ON-CONFLICT create-path** conversion for `seed`/`set_cron` stays **owed** (deferred this slice; the reconciler slice still needs it — see Open questions).

The schedule is **data**, mutated instantly via `carve schedule` (and the equivalent REST/MCP surface wired in spec 09). `src/carve/cli/commands/schedule/` ships:

```
carve schedule list                         # all schedules with cron, timezone, paused, last/next fire
carve schedule show <pipeline>
carve schedule pause <pipeline> [--reason]
carve schedule resume <pipeline> [--reason]
carve schedule set-cron <pipeline> "<cron>" [--timezone TZ] [--target-pipeline TARGET] [--reason]
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

> **Updated during implementation (2026-06-26, heartbeat+reaper slice):** `JobQueue` gained **`reclaim_stale(now, *, stale_threshold_s=60.0, tenant_id=1) -> list[(id, run_id, prior_claimed_by)]`** — the reaper's atomic CTE reclaim (see §Reaper) — so its `update_heartbeat` column is now read by a live reaper. And the three claim-owning writes — **`transition_to_running`, `mark_finished`, `update_heartbeat`** — gained an optional **`expected_worker_id`** that turns the write into a **guarded/conditional** `UPDATE ... WHERE claimed_by = :worker_id` (the **ownership guard**; see the §Heartbeats callout). A returning zombie worker's guarded write matches 0 rows and is a **silent no-op** (`transition_to_running`/`mark_finished` now return `bool`; `update_heartbeat` returns early) — no double-run, no status stomp. `expected_worker_id=None` preserves the prior unconditional behavior (back-compat). The `job.reclaimed` audit rides the new no-op `JobQueue._emit` seam (events table still deferred).

> **Shipped during implementation (events slice, 2026-06-29):** `JobQueue.__init__` gained an optional `emitter: EventSink | None = None` (default `None` ⇒ the prior silent no-op, back-compat) and its `_emit` delegates to it. The existing transition points now persist durable rows when an emitter is injected: `job.queued` (`enqueue_scheduled`/`enqueue_manual`), `job.claimed` (`claim_next`), `worker.registered`/`worker.unregistered` (`register_worker`/`unregister_worker`), and the reaper's `job.reclaimed` (`reclaim_stale` → `_emit`). No new transitions were added.

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

> **Shipped during implementation (worker-placement slice, 2026-07-01):** `claim_next` gained a keyword-only `worker_label: str | None = None` and **one more** inner-`SELECT` predicate — `AND (required_label IS NULL OR required_label = :worker_label)` (a bound `:worker_label` param mirroring the `:now`/`:tenant_id` style, **never interpolated**) — so a labeled job (`required_label = 'X'`) is claimed **only** by a matching worker, an **unlabeled** worker (`worker_label=NULL`) claims **only** unlabeled jobs (SQL-NULL: `required_label = NULL` is never true), and unlabeled jobs run anywhere. The filter is in the `WHERE`, so `ORDER BY … LIMIT 1` still picks the oldest *eligible* row (no head-of-line blocking). `worker_label=None` (the default) leaves the query byte-identical to the flat pool (back-compat). See §"Worker placement & labeling".

### Worker loop

> **Updated during implementation (2026-06-26):** a **minimal** worker shipped at `src/carve/runtime/worker.py` — the smallest thing that closes claim → run → persist. The shape differs from the sketch below: a `run_once(ctx)` coroutine claims and runs **at most one** job (returning whether one ran), and `worker_loop(ctx, …)` polls `run_once` on an interval until a `shutdown` event is set, registering a `workers` row on entry and unregistering on exit. State flows through a `WorkerContext` dataclass (the sync `Repository` + `JobQueue`, `ProjectPaths`, connections, dbt executable, and an injectable `registry_factory` for creds-free tests). It builds the run via `execute_pipeline(..., sink=PersistingStepSink(run_id, job_queue))` over a freshly built `dlt→dbt→sql` registry (`build_step_executor_registry`). **The load-bearing safety property:** once a job is claimed it is the worker's, so **any** failure after the claim — a setup DB error (create-run / transition / status write) just as much as an execute error — marks the job **and** run `failed` (best-effort), so a claimed job is never orphaned. This matters precisely because **the reaper that would otherwise reclaim a stuck job is DEFERRED** this slice. `PipelineAlreadyRunning` is the one non-failure exit: the claim is released and the run cancelled. **DEFERRED:** the **heartbeat *loop*** (the `heartbeat_at` column ships and is stamped once at claim, but no `HeartbeatHandle`/background beat), the **reaper**, **crash recovery**, and the worker-pool fan-out (`--workers N` > 1 is rejected). `execute_pipeline` is the already-shipped pipelines entry point (Increment 3), not a spec-08 future. The original async sketch below is retained as design intent.

> **Shipped during implementation (events slice, 2026-06-29):** `WorkerContext` gained an optional **`emitter: EventSink | None`** and an optional **`on_run_failed: LifecycleHook | None`**. At its existing run-lifecycle transitions the worker now emits durable events when an emitter is injected — `run.started` (after the `running` status write), `run.succeeded`/`run.failed` in `_finalize` (executed-then-finished), and `run.failed` in `_fail_job` (setup-exception) — each bridged via `asyncio.to_thread`, no-op without an emitter. **The runtime worker is the `on_run_failed` hook's emitter:** at the same two `run.failed` transitions (executed-then-failed + setup-exception; **NOT** the reaper's crash-reclaim path — that stays the recovery engine's domain) it fires the user `on_run_failed` hook through the gated `HookRunner` at **`PermissionMode.DEPLOY`**, with **post-event semantics** — the run already failed, so a raising/denied hook is logged, not fatal, and the run stays terminal-failed (the `post_build` post-commit stance). The durable `run.failed` event and the user `on_run_failed` hook are **two distinct mechanisms** that both fire here, neither calling the other.
>
> **Forward note (the `on_run_failed` hook is inert until a runtime approver lands).** `DEPLOY` is the **narrowest** mode that admits a network-reaching notify command at all (raw `curl`/`wget` are denied at every mode; only the deploy-tier prompt set — `gh` / `git push` / `dlt deploy` — reaches the network, and only via an approver prompt). The CLI wires **no approver**, so a deploy-tier command **fails closed (denied)** today — the archetypal `notify-slack` hook can't yet egress. When a runtime approver is wired, it **must NOT auto-approve deploy-tier commands for lifecycle hooks** (else an `on_run_failed` hook could auto-push on every failed run) — or the mode must be narrowed at that point.

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

> **Shipped during implementation (worker-pool slice, 2026-06-29):** the worker pool **SHIPPED** at `src/carve/runtime/worker_pool.py`. `run_worker_pool(ctx, *, workers, shutdown, force=None, grace_period_s=DEFAULT_GRACE_PERIOD_S=300.0, poll_interval_s=DEFAULT_POLL_INTERVAL_S)` spawns N `worker_loop` tasks with unique **`:taskN`-suffixed** `worker_id`s (carved off the base id via `dataclasses.replace`), all sharing the ONE `JobQueue`/`EventEmitter`/registry/session pool — the queue's `FOR UPDATE SKIP LOCKED` claim is the only coordination point (no second claim path). **The load-bearing design call:** the N tasks join under **`asyncio.gather(*tasks, return_exceptions=True)`** (NOT a bare `TaskGroup`), so a worker that escapes its own guard is logged-at-error + dropped (`_log_worker_crashes`) without cancelling its siblings or serve's scheduler/reaper/archiver; the pool itself is a single `tg.create_task` child of serve's group (one TaskGroup child, but its internal join is the isolating `gather`). Restarting a crashed worker is out of scope this slice — log + drop is the contract. `runtime/worker.py` was **not** changed — the per-worker drain (`worker_loop` stops claiming the instant its shared `shutdown` is set, finishes its in-flight `run_once` **un-cancelled**, and `unregister_worker`s in `finally` even on cancel) was already complete.

`src/carve/runtime/worker_pool.py` spawns N `worker_loop` tasks as asyncio coroutines within a single Python process. All share the same event loop, same DB connection pool, same registered worker_ids (each gets a unique id with a `:taskN` suffix).

For users who want cross-machine scaling: separate `carve worker` processes (next section) coordinate via the same Postgres queue. The architecture is identical whether workers live in one process or many — the queue is the only coordination point.

### Worker placement & labeling

> **Shipped during implementation (worker-placement slice, 2026-07-01):** worker placement **SHIPPED** — the **7th and final Increment-4 runtime slice** (un-defers the "worker-placement labeling (`--label`)" item the worker-pool-slice callouts marked DEFERRED). **Placement is per-pipeline-run** (one job = one whole-DAG run on one worker), so a job's **`required_label`** is a **derived reduction** over the pipeline's referenced components' `worker_label`s — **0** labeled components → `NULL` (runs anywhere, the flat pool), **exactly 1** distinct label → that label, **≥ 2** distinct labels → a typed **`ConflictingWorkerLabelsError`** (a `PipelineError` subclass) **rejected at `load_pipeline` author-time** (a mis-labeled pipeline fails to load in `pipeline verify` / build / serve, never enqueued). The pure reducer is **`resolve_required_label(pipeline, components)`** (`core/config/pipeline_schema.py` — placed in core, not runtime, so `load_pipeline` in the same module can call it without a `core → runtime` import), the single source called by **both** `load_pipeline` (reject) and the scheduler's enqueue resolver (stamp) — one reduction, no duplication.
> - **The claim is label-filtered.** `claim_next(worker_id, *, worker_label=None, tenant_id=1)` adds the bound-param predicate `AND (required_label IS NULL OR required_label = :worker_label)` to the inner `SELECT` (SQL-NULL semantics: an unlabeled worker claims **only** unlabeled jobs — `required_label = NULL` is never true, so only the `IS NULL` branch matches; a labeled job is claimed **only** by a matching worker; unlabeled jobs run anywhere). Ordering / `scheduled_for` window / `FOR UPDATE SKIP LOCKED` / the one-queued & one-running invariants are untouched; `worker_label=None` (the default) makes the flat pool byte-identical (back-compat).
> - **Enqueue wiring.** `enqueue_scheduled`/`enqueue_manual` gain `required_label` (stamped into the INSERT column list; the manual upsert also refreshes it via `EXCLUDED.required_label`). The **scheduler** takes a `resolve_label` callback (default `None` → existing scheduler tests byte-identical); a resolver that raises skips only its own fire (a per-schedule `try/except`, `next_fires_at` left un-advanced → retried next boundary), never starving the other due fires. **`carve serve`** builds the resolver — `resolve_worker_label(pipeline_name, *, project_paths, components)` — from `config.components` + `ProjectPaths` and threads it into `scheduler_loop`, with a **path-containment guard** (`.resolve()` + `parent == pipelines_dir`, reusing the `pipeline_inspect` pattern) so an unvalidated `schedule.pipeline` (a `carve schedule set-cron` / DB string) can't escape `pipelines/` and open an arbitrary `.toml` — it returns `None` **without ever opening the file**; a per-pipeline resolve failure logs + returns `None` (unlabeled), never blocking other pipelines' fires. `enqueue_manual` has **no production caller** today — the stamp is wired + repo-tested for forward-compat; the scheduler is the only live driver.
> - **Workers advertise a label.** `WorkerContext.label` threads into `claim_next(worker_label=)` + `register_worker(label=)`; `carve worker --label` and `carve serve --label` set it; the pool's `replace(...)` preserves it for free, and the loop's `_with_worker_id` field-by-field rebuild copies it (load-bearing — omitting it would silently drop the label on the id-rebind).
> - **Schema.** Migration **`0013_runtime_worker_placement`** adds `required_label` to **both** `jobs` and `jobs_archive` — the `LIKE`-clone (0011) is a one-time copy that doesn't track later `ALTER`s, so both must be added to keep the column-order parity the archiver's `INSERT … SELECT *` relies on (a cross-slice invariant, now regression-tested). **No new index** (the claim rides the existing partial claim index; `required_label` is a low-cardinality filter over the already-tiny queued set). `ComponentConfig.worker_label` is flipped from "accept + store only" to **live-read** (field + validation unchanged; the mechanism reads it now).
>
> **Deferred / fenced (honest — not shipped):** **step-level `worker_label`** (the `extra="forbid"` step models) + a step-over-component precedence rule; **`sql`-step labels** (a `sql` step references no component); any **new `worker_label` field on the dlt/sql component configs** (the shared `ComponentConfig` field already exists — the *mechanism* is general/serves dlt/sql too, only the *config source* is component-scoped today). The serve resolver **re-parses `load_pipeline` per due fire** (a cache-by-path+mtime is a noted future optimization, fine at current scale). **Recovery auto-resume** (the `paused_by='recovery'` mutators, §"Pause origin and recovery auto-pause/auto-resume") stays **Increment 6**. With this slice **runtime's Increment-4 scope is complete** — only those recovery mutators remain referenced in this spec.

By default any worker claims any job (the flat `FOR UPDATE SKIP LOCKED` pool). But some steps must run **in a specific place** — e.g. a `dbt` step whose [execution backend](./dbt-execution.md) is `local` and must run on the team's own dbt server (its VPC reach + pinned dbt env), or a `dlt` step that must run next to a locked-down source. For these, a worker **advertises labels** (`carve worker --label onprem-dbt`) and a component/step can **require** one (`worker_label = "onprem-dbt"`). The claim query filters by label, so a labeled job is only picked up by a matching worker; unlabeled jobs run anywhere. This is the standard self-hosted-runner pattern (GitHub runner labels, k8s node-selectors) — it's how "run dbt on our own server" (the co-located-worker case) and any near-the-data execution is expressed, and it's **general** (serves `dlt`/`sql` too), not dbt-specific.

### Heartbeats

> **Updated during implementation (2026-06-26, heartbeat+reaper slice):** the **heartbeat loop SHIPPED** at `src/carve/runtime/heartbeat.py`. Shipped shape vs. the sketch below: `start(job_queue, job_id, *, interval_s=10.0, clock=system_clock, worker_id=None) -> HeartbeatHandle` takes the **`job_queue`** explicitly (not a module global) and the **`Clock` seam** (`runtime/clock.py` — `system_clock` in production, `FakeClock` in tests, so the loop drives sleep-free) and threads `worker_id`; `HeartbeatHandle.stop()` cancels the task cleanly and is **idempotent** (a second call no-ops). The worker starts the loop **after `transition_to_running`** and stops it in a `finally`, so a beat can never leak past job completion. Each beat bridges the sync `job_queue.update_heartbeat` off the event loop via `asyncio.to_thread` (the same sync↔async seam as `worker.py`/`scheduler.py`), passing **`expected_worker_id=worker_id`** — the **ownership guard** below — so a returning zombie's beat no-ops on a job the reaper already reclaimed. The beat sleeps to the next `interval_s` boundary (`clock.sleep_until_next_boundary`), not `now + interval_s`. The sketch below is retained as design intent.

`src/carve/runtime/heartbeat.py` (original design sketch):

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

Heartbeats are best-effort: a transient DB failure (a beat that raises) is **logged and swallowed** — it doesn't kill the loop or the worker; it just means a missed beat. The reaper's threshold (60s) is set to tolerate ~5 consecutive missed beats before reclaiming.

> **The ownership guard (SHIPPED — a correctness addition the sketch didn't spell out).** `update_heartbeat`/`transition_to_running`/`mark_finished` gained an optional `expected_worker_id`. When supplied, the write is a guarded/conditional `UPDATE ... WHERE ... claimed_by = :worker_id` (atomic, no read-then-write window): a worker that stalled past the reaper threshold, was reclaimed (its job returned to `queued` / re-claimed by a peer), and then *returns* matches **0 rows** — a **silent no-op** (no double-run, no status stomp; the returning zombie backs off). `expected_worker_id=None` preserves the prior unconditional behavior (back-compat for existing callers/tests). The worker threads `worker_id` as `expected_worker_id` into **every** state write (uniform ownership-awareness); a lost-claim `transition_to_running` (returns `False`) cancels the orphaned run instead of executing. This is the zombie-worker-no-stomp boundary the crash-recovery story needs.

### Reaper

> **Updated during implementation (2026-06-26, heartbeat+reaper slice):** the **reaper SHIPPED** at `src/carve/runtime/reaper.py`, factored (like the scheduler) into a synchronous deterministic single pass + an async boundary loop: `reap_stale_once(job_queue, repository, now, *, stale_threshold_s=60.0, tenant_id=1) -> list[str]` (driven sleep-free under a `FakeClock` in tests) and `reaper_loop(job_queue, repository, *, interval_s=30.0, stale_threshold_s=60.0, clock=system_clock, shutdown=None, tenant_id=1)` (the loop `carve serve` hosts; bridges `reap_stale_once` off the event loop via `asyncio.to_thread`, sleeps boundary-aligned, swallows a per-pass error, races its sleep against `shutdown` for a prompt stop). The reclaim is **`JobQueue.reclaim_stale(now, *, stale_threshold_s, tenant_id)`** — **ONE atomic statement** (`WITH stale AS (SELECT ... FOR UPDATE SKIP LOCKED) UPDATE jobs SET status='queued', claimed_by=NULL, ... FROM stale WHERE jobs.id = stale.id RETURNING jobs.id, jobs.run_id, stale.claimed_by`) so two reapers can't double-reclaim. Two deltas vs. the sketch below worth recording as deliberate corrections: **(1)** the stale cutoff is a **bound `:cutoff` param** computed in Python (`now - stale_threshold_s`), **not** the sketch's interpolated `INTERVAL ':stale_threshold seconds'` literal — a deliberate injection-avoiding correction that mirrors `claim_next`'s bound-param style; **(2)** the CTE **snapshots the PRIOR `claimed_by`** (the post-`UPDATE` `claimed_by` is already NULL, so a plain `RETURNING claimed_by` would return NULL — the CTE captures it before the flip) for the `job.reclaimed` audit. The reaper then, per reclaimed job with a non-NULL `run_id`, fails the orphaned in-flight Run via `repository.update_run_status(run_id, 'failed', 'worker_crashed_or_unreachable')`, and emits `job.reclaimed` through the queue's **no-op `_emit` seam** (`events` table still deferred). The sketch below is retained as design intent.

> **Shipped during implementation (events slice, 2026-06-29):** the reaper's `job.reclaimed` emit is now **live** — `JobQueue._emit` delegates to the injected `EventSink` (`carve serve` wires the emitter into the `JobQueue` the reaper shares), so each reclaimed job persists a durable `job.reclaimed` row (`job_id`, `prior_claimed_by`, `reason`). The crash-reclaim path emits the durable event but **does not** fire the user `on_run_failed` hook (recovery's domain — see §Worker loop).

`src/carve/runtime/reaper.py` runs alongside the scheduler (original design sketch):

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
- Have any in-flight Run row marked `status='failed'` with `error_message='worker_crashed_or_unreachable'` (a job reclaimed before it ever transitioned has `run_id IS NULL` and is skipped here)
- Are re-claimable by the next available worker
- Their step-level state is discarded (the next worker runs the pipeline from scratch)

### Archiver

> **Shipped during implementation (archiver slice, 2026-06-29):** the **archiver SHIPPED** at **`src/carve/runtime/archiver.py`**, factored exactly like the reaper into a sync deterministic pass + an async boundary loop:
> - **`archive_table_safely(session_factory, table, *, cutoff, status_filter, finished_col, tenant_id=1, tenant_scoped=True) -> int`** — the **single-transaction verify-then-delete** (the load-bearing no-data-loss invariant): in ONE `with session_factory() as session:` it counts the active rows matching the batch predicate → `INSERT INTO <t>_archive SELECT * FROM <t> WHERE <predicate>` → **verifies `inserted == expected` BEFORE any DELETE** (a mismatch raises `ArchiveVerificationFailed`) → `DELETE` → verifies `deleted == expected` → `commit`. **Any** exception (a mismatch, an injected error, an FK/IO error) propagates out of the `with`, rolls the whole batch back, and the active table is **never short a row** (under the single transaction the INSERT rolls back too — the asserted bar is *no data loss / active intact*, not literally "archive has the rows"). Raw `sa.text` + bound params, the `reclaim_stale` style.
> - **`archive_once(session_factory, now, config, *, emitter=None, tenant_id=1) -> dict[str, int]`** — one deterministic sync pass over the four tables, **children before the parent `runs`** (FK-safe: `jobs`/`step_runs`/`logs` all FK → `runs.id`), computing `cutoff = now - window` per table and emitting `archive.batch_completed` per processed table; a per-table failure is **logged and the pass continues** to the next table.
> - **`archiver_loop(session_factory, config, *, interval_s=DEFAULT_ARCHIVE_INTERVAL_S, clock=system_clock, emitter=None, shutdown=None, tenant_id=1)`** — the async boundary loop `carve serve` hosts, **the exact `reaper_loop` shape**: bridge `archive_once` off the event loop via `asyncio.to_thread`, sleep `clock.sleep_until_next_boundary` **raced against `shutdown`**, swallow a per-pass error. Default interval **3600s** (`DEFAULT_ARCHIVE_INTERVAL_S`).
> - **`ArchiveVerificationFailed`** is defined here.
>
> **The per-table predicate map is load-bearing — the single generic `finished_at`/`status` of the sketch below does NOT apply uniformly:** `jobs` → age `finished_at`, status `{succeeded, failed, cancelled, timed_out}`; **`runs` → age `completed_at`, status `{success, failed, cancelled, crashed}` — the M1 run vocabulary writes `"success"` (NOT `"succeeded"`; the worker maps `_RUN_STATUS_BY_RESULT = {"succeeded": "success", …}` and a crash terminates a run as `"crashed"`), so the archiver MUST use `"success"` or successful runs never archive** (a fix-iteration catch); `logs` → age `timestamp`, **no status** (age-only); `step_runs` → age `finished_at`, status `{succeeded, failed, skipped}`. **`tenant_scoped` reconciliation:** only `jobs` carries `tenant_id`, so only its predicate adds the `tenant_id` clause; `runs`/`logs`/`step_runs` (pre-multi-tenancy M1 tables) archive **globally** (safe single-tenant — the predicate must add the scope when those tables gain `tenant_id` in hosted). `cutoff` is a **Python-computed bound param** (`now - window`, never SQL `now()`); `now` is injected so the window/terminal-status-filtering tests are deterministic (a `FakeClock` / fixed `now`).
>
> **Config — the FIRST `runtime.toml` surface shipped:** `[runtime.archive]` via **`ArchiveConfig`** (`interval_s: int = Field(3600, ge=1)` + `jobs_window`/`runs_window`/`logs_window`/`steps_window` duration strings, each parsed by `parse_duration` → `timedelta` and **validated strictly-positive** at load → `ConfigError`) under **`RuntimeConfig`** (`[runtime]`), wired into `Config.runtime`; `loader.py` added `runtime.toml` to its section set. The toml block below is the live surface. The async/asyncpg-flavored sketch below is retained as design intent.

`src/carve/runtime/archiver.py` runs on its own hourly loop (original design sketch):

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
>
> **Shipped during implementation (events slice, 2026-06-29):** the paired `step.*` emit is now **live** — on `step_started` the sink emits `step.started`, and on `step_finished` it emits `step.completed`/`step.failed` (keyed on `result.status`; payload `step_run_id` / `run_id` / `type` + `outputs` on success / `error_message` on failure) **through the queue's `_emit` seam** (`self._job_queue._emit(...)`, reusing the queue's injected emitter — no new constructor param on the sink), a silent no-op when no emitter is injected. (The "stays a no-op seam" note above is now superseded.)

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

> **Updated during implementation (worker-pool slice, 2026-06-29):** `carve serve` now hosts **scheduler + reaper + archiver + worker pool** — **four** co-running concerns in the one `asyncio.TaskGroup` under the single shutdown `Event` (this supersedes the 3-loop form in the callout immediately below). The pool is added as a 4th `tg.create_task`, **gated** on `worker_ctx is not None and workers >= 1` (so the direct-`_serve` unit tests, which pass no ctx, keep running scheduler + reaper + archiver only); `--workers N` defaults to **1**. The **graceful drain shipped as built** (3 phases): set the shared `shutdown` Event (each `worker_loop` already stops claiming, finishes its in-flight `run_once` un-cancelled, and `unregister_worker`s in `finally`) → wait up to the **grace period** (`--drain-timeout`/`--grace-period`, default 300s) for the pool to drain → on grace-expiry **or** a **second signal** (`force`), `.cancel()` the stragglers. The `CancelledError`, being a `BaseException`, bypasses `run_once`'s `except Exception` and reaches the worker's `finally`, so the worker still unregisters and the interrupted in-flight job is left `running`/stale for the **reaper** to reclaim — no loss, no double-complete; the inherited `expected_worker_id` ownership guard prevents a zombie-stomp. The **signal handler is now stateful** (`_serve._on_signal`): the 1st SIGINT/SIGTERM sets `shutdown`, a 2nd sets `force` — replacing the prior `add_signal_handler(sig, shutdown.set)`. `serve` also grew the **`WorkerContext` setup block** it previously lacked (`ProjectPaths.from_root` / `config.connections` / the default dbt executable / the `on_run_failed` hook via `build_extensibility_on_run_failed_hook` at `PermissionMode.DEPLOY`), lifted from `carve worker`. **STILL DEFERRED:** FastAPI/`--port`/`--host`, leader-election, and the auto-migrate/token-bootstrap startup sequence — all retained as design intent in the sketch below.
>
> **Updated during implementation (archiver slice, 2026-06-29):** `carve serve` now hosts **scheduler + reaper + archiver** — **three** co-running loops in the one `asyncio.TaskGroup` under the single shutdown `Event` (this supersedes the 2-loop form in the callout immediately below). The archiver is added as a 3rd `tg.create_task`, constructed from the already-built `session_factory` + the shared `EventEmitter` + `config.runtime.archive`; new flags **`--no-archiver`** (skip the 3rd task) and **`--archive-interval`** (default `DEFAULT_ARCHIVE_INTERVAL_S`, overrides `config.runtime.archive.interval_s`). The **full supervisor stays DEFERRED** — the worker-pool fan-out (`--workers N`), FastAPI, leader-election, and the in-flight-drain grace period below all remain design intent.
>
> **Updated during implementation (2026-06-26, heartbeat+reaper slice):** `carve serve` now runs **TWO co-resident loops** — `scheduler_loop` **+** `reaper_loop` — under **one** shutdown `asyncio.Event`, in an `asyncio.TaskGroup` (a fatal error in either loop cancels the other; each loop already swallows per-pass errors). It now constructs a `Repository` alongside `JobQueue` + `Schedules` (the reaper needs it to fail orphaned in-flight runs), and gained a **`--reaper-interval`** flag (default 30s) beside `--interval`. The **FULL supervisor stays DEFERRED**: archiver, the worker-pool fan-out (`--workers N`), FastAPI, leader-election, and the in-flight-drain grace period below — all retained as design intent. This supersedes the scheduler-only form noted in the callout immediately below.
>
> **Updated during implementation (2026-06-26, scheduler slice):** a **minimal, scheduler-only** `carve serve` shipped at **`src/carve/cli/commands/serve.py`** — it runs JUST the `scheduler_loop` as a single asyncio task with graceful shutdown (SIGINT/SIGTERM set an `asyncio.Event`; falls back to `KeyboardInterrupt` where signal handlers can't be installed), over the same setup block as `carve worker` (`load_config` → resolve active target → engine → `initialize_database` → `JobQueue` + `Schedules`). Its only flag this slice is **`--interval`** (default 30s). *(Superseded by the heartbeat+reaper-slice callout above — serve now hosts scheduler + reaper.)* **DEFERRED:** the full multi-loop **SUPERVISOR** (archiver + worker pool + FastAPI + leader-election), every `--port`/`--host`/`--workers`/`--no-*` flag, the auto-migrate/token-bootstrap startup sequence, and the in-flight-drain grace period below — all retained as design intent. The startup/shutdown sequences below are the supervisor's target design, not what the shipped serve does.

`src/carve/cli/serve.py` (original design sketch — the full supervisor):

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

> **Updated during implementation (worker-pool slice, 2026-06-29):** `carve worker --workers N` is now **ENABLED** — the `>1` rejection dropped. Without `--once` the command drives **`run_worker_pool`** (the same pool `carve serve` uses) over one base `WorkerContext` shared across the N tasks, wiring the same **stateful 1st/2nd-signal handler** (1st SIGINT/SIGTERM → graceful drain; 2nd → `force`/skip-grace). `--once` stays the single-job, single-worker path (one `run_once`, no pool). The `--workers` guard now rejects only **non-positive** counts (`< 1` → exit 2). (Supersedes the "single worker in this slice" / `>1`-rejected note below.)
>
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

> **Shipped during implementation (events slice, 2026-06-29):** the emitter **shipped** at `src/carve/runtime/events.py` — **`EventEmitter`** (writes one durable `events` row per `emit(kind, payload, *, tenant_id=1)`; **sync**, like the rest of the state store, bridged off the event loop via `asyncio.to_thread`; **best-effort** — an emit failure is logged-and-swallowed so a down DB never kills a run/loop, mirroring the heartbeat's stance) plus the **`EventSink` Protocol** (defined in `runtime/events.py` and referenced under `TYPE_CHECKING` in `core/state/job_queue.py` / `schedules.py` to dodge the `carve.runtime` circular import). The wiring: `JobQueue`, `Schedules`, and `WorkerContext` each take an optional **`emitter: EventSink | None = None`** (default `None` ⇒ silent no-op, back-compat for every existing caller/test); the concrete `EventEmitter` is constructed + injected at the **`carve worker` / `carve serve` / `carve schedule`** CLI sites. With it the taxonomy table below is now the **live contract** — the in-scope events that have a transition point today persist: `schedule.skipped|paused|resumed|changed|seeded`, `job.queued|claimed|reclaimed`, `run.started|succeeded|failed`, `step.started|completed|failed`, `worker.registered|unregistered`. **`archive.batch_completed`** (archiver) and **`schedule.reseeded`** (the spec-08 reconciler) stay deferred with their slices. **Two distinct mechanisms fire at `run.failed`** — the durable `run.failed` *event* (observability/webhook substrate, written via the emitter) and the user **`on_run_failed` hook** (a gated shell command; see §Worker loop) — neither calls the other; the worker drives both at the same transition.
>
> **Forward note (events/UI surfacing slice):** the durable rows carry step `outputs` / `error_message` and run `error_message` **verbatim** — the later slice that first *surfaces* `events` to users (REST/webhooks/UI) owns **redaction at the surfacing boundary** (step outputs/errors can carry secrets). Recorded here so that slice owns it.
>
> **Shipped during implementation (archiver slice, 2026-06-29):** **`archive.batch_completed`** (payload `table`, `rows_moved`) is now **emitted durably** — once per processed table per archiver pass, through the same injected `EventSink`/`_emit` seam (`archive_once(…, emitter=…)`; a silent no-op without one, the back-compat default). It **leaves the deferred set** named in the events-slice callout above; only `schedule.reseeded` (the spec-08 reconciler) stays deferred now.

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
> > **Updated during implementation (2026-06-26, scheduler slice):** the **scheduler slice** shipped tests for the cron math / scheduler / schedules repo / `carve schedule` / minimal `carve serve` / migration 0009 it delivered. Shipped this slice:
> - **Unit (cron):** `*/5` ticks, strictly-after-an-exact-tick, inclusive `this_tick_at`, UTC-aware results, schedule-timezone evaluation, **DST spring-forward (no double/skip) + fall-back (fires once)**, and the **unsatisfiable cron → typed `CronError`** (not a croniter traceback) (`tests/runtime/test_cron.py`).
> - **Unit (scheduler):** a due schedule fires exactly once; a second pass in the same window **skips, no double-enqueue**; a fire **advances `next_fires_at`** so it doesn't re-fire; a paused row is skipped; a 20-minute clock jump produces **one** fire, not four; `scheduler_loop` is deterministic + boundary-aligned under `FakeClock` and stops on shutdown without firing (`tests/runtime/test_scheduler.py`).
> - **Unit (schedules repo):** `seed` creates + is idempotent-upsert; `list_due` returns only due/unpaused (and excludes `next_fires_at IS NULL`); `set_last_fired` advances to the following tick + leaves the row not-due; `pause`/`resume` set/clear origin + append audit; `set_cron` recomputes + audits + UPSERTs when absent; **the `ck_schedules_pause_origin` CHECK rejects a paused-NULL-origin row, rejects an active-non-NULL-origin row, and accepts the `recovery` origin** (`tests/runtime/state/test_schedules_repository.py`).
> - **CLI (`carve schedule`):** `set-cron` creates + audits; bad cron / bad timezone / unsatisfiable → **exit 2 (no write)**; `pause`/`resume` mutate + audit; unknown pipeline → exit 1; `list`/`show` render (`tests/cli/commands/test_schedule_command.py`).
> - **CLI (`carve serve`):** the scheduler-only serve runs the loop + stops on shutdown; help describes scheduler-only; bad config → exit 2 (`tests/cli/commands/test_serve_command.py`).
> - **Migration 0009:** creates the schedule tables + indexes + CHECK; the **raw-SQL** pause-origin CHECK rejects an inconsistent state; downgrade drops both (`tests/migrations/test_migrations.py`).
>
> > **Updated during implementation (2026-06-26, heartbeat+reaper slice):** the **heartbeat+reaper slice** shipped tests for the heartbeat loop / reaper / atomic `reclaim_stale` / the ownership guard / the 2-loop `carve serve` it delivered. **No migration this slice** (`heartbeat_at` + `ix_jobs_heartbeat_at` shipped in 0008). Shipped this slice:
> - **Unit (heartbeat):** the loop stamps `heartbeat_at` on its interval under a `FakeClock`; a transient `update_heartbeat` failure is survived (logged + swallowed, loop continues); `HeartbeatHandle.stop` cancels cleanly **and is idempotent** (`tests/runtime/test_heartbeat.py`).
> - **Unit (reaper):** `reap_stale_once` reclaims a stale job, fails its in-flight run (`worker_crashed_or_unreachable`), and emits `job.reclaimed`; a reclaimed job with `run_id IS NULL` skips the run-fail; `reaper_loop` runs a pass then stops on shutdown, and stops immediately on a preset shutdown (`tests/runtime/test_reaper.py`).
> - **Unit (atomic reclaim_stale):** the `reclaim_stale` CTE reclaims only `heartbeat_at < now - threshold` `claimed`/`running` jobs (a fresh-beat job is left alone), returns the **prior** `claimed_by` (snapshotted, not the post-UPDATE NULL) + `run_id`, and is two-reaper-safe (`tests/runtime/state/test_job_queue_reaper.py`).
> - **Unit (ownership guard):** `transition_to_running`/`mark_finished`/`update_heartbeat` with `expected_worker_id` no-op (return `False` / early) on a job no longer claimed by that worker — the returning-zombie no-stomp boundary; `expected_worker_id=None` preserves unconditional behavior (`tests/runtime/state/test_job_queue_ownership_guard.py`).
> - **Integration (worker + heartbeat/ownership):** the worker starts the heartbeat after `transition_to_running` and stops it in a `finally`; a lost-claim transition cancels the orphaned run instead of executing (`tests/runtime/test_worker_end_to_end.py`).
> - **CLI (`carve serve`):** the 2-loop serve runs scheduler + reaper and stops both on shutdown; `--reaper-interval` is accepted (`tests/cli/commands/test_serve_command.py`).
>
> > **Updated during implementation (archiver slice, 2026-06-29):** the **archiver slice** shipped tests for the verify-then-delete archiver / migration 0011 / the 3-loop `carve serve` / the `[runtime.archive]` config it delivered (the **archiver** bullets below are now **SHIPPED**):
> - **Unit (archiver):** N terminal rows older than the window archive into `*_archive` (count matches, active rows deleted; `jobs` end-to-end); window + terminal-status filtering leaves fresh / non-terminal rows; per-table predicate correctness (`jobs.finished_at`, `runs.completed_at` + the `"success"` vocab, `logs.timestamp` age-only, `step_runs.finished_at`); `archive_once` emits `archive.batch_completed` per table **only** when an `EventSink` is injected (`tests/runtime/test_archiver.py`).
> - **Integration (verify-then-delete, no data loss):** an injected failure between INSERT and DELETE leaves the active table intact (no row lost); a count-mismatch raises `ArchiveVerificationFailed` and halts the batch atomically — nothing deleted, nothing half-moved (same file).
> - **Migration 0011:** create (4 archive tables + 4 indexes) + downgrade (drops them, restores 0010) (`tests/migrations/test_migrations.py`).
> - **CLI (`carve serve`):** the 3-loop serve runs scheduler + reaper + archiver and stops all on shutdown; `--archive-interval` accepted; `--no-archiver` skips the archiver loop (`tests/cli/commands/test_serve_command.py`).
> - **Config:** `[runtime.archive]` parses (windows + interval) + defaults apply when absent + a non-positive / unparseable window or interval is rejected (`ConfigError`) (`tests/core/config/`).
>
> > **Updated during implementation (worker-pool slice, 2026-06-29):** the **worker-pool slice** shipped tests for the fan-out + graceful drain it delivered, realized as **deterministic in-process** tests (N `worker_loop` coroutines sharing one Postgres + `FakeClock`/injected-registry — the spec's "spawn N processes / `kill -9`" cases collapse, no real subprocesses, no wall-clock sleeps). **No migration this slice.** Shipped this slice:
> - **Unit/integration (none-twice fan-out):** N in-process workers (unique `:taskN` ids) drain M queued jobs; every job reaches a terminal state **exactly once** (M distinct terminal jobs/runs); the N worker rows are registered then unregistered (`test_pool_drains_every_job_exactly_once`).
> - **Integration (graceful drain waits for in-flight):** with a worker mid-`run_once` (a slow/blocking injected step), setting `shutdown` lets the in-flight job **complete** (terminal, not reclaimed) and then the pool exits + unregisters within the grace window (`test_graceful_drain_waits_for_in_flight_job`).
> - **Integration (second signal skips grace):** with an in-flight job and `shutdown` set, firing `force` **cancels** the worker mid-run — the pool exits promptly (well under `grace_period_s`), the in-flight job is left non-terminal for the reaper, and the worker still unregisters (`test_second_signal_skips_grace_and_cancels`).
> - **Integration (per-task crash isolation):** one worker task is made to raise; its siblings keep draining the remaining jobs and the pool still completes/unregisters (proves `gather(return_exceptions=True)`, not a bare `TaskGroup` that would cancel the siblings + serve's loops) (`test_one_worker_crash_does_not_take_down_siblings`).
> - **CLI (`carve serve`):** `_serve(..., worker_ctx=…, workers=2)` runs the pool alongside scheduler + reaper + archiver and stops **all** on shutdown; `--help` now lists `--workers` + `--drain-timeout` (`tests/cli/commands/test_serve_command.py`).
> - **CLI (`carve worker`):** `carve worker --workers 2` accepts/runs N — drains a queued job and exits 0 on Ctrl-C — replacing the old `--workers > 1` rejection test (`tests/cli/commands/test_worker_command.py`).
>
> > **Shipped during implementation (worker-placement slice, 2026-07-01):** the **worker-placement slice** shipped the placement tests — a **backfilled subset** (the spec had **none**; the design lived only in the §"Worker placement & labeling" paragraph). **Migration 0013** this slice. Shipped:
> - **Unit (claim label matrix)** (`tests/runtime/state/test_job_queue_placement.py`): a labeled worker claims a matching labeled job **and** unlabeled jobs; an unlabeled worker claims **only** unlabeled jobs (the SQL-NULL semantics asserted — a labeled job stays unclaimed, `claimed_by is None`); a labeled job **stays `queued`** for a non-matching/unlabeled worker and is later claimed by a matching one; `enqueue_scheduled`/`enqueue_manual` stamp the passed `required_label` (the manual upsert refreshes it via `EXCLUDED`, incl. clearing it), default `None` → column NULL.
> - **Unit (label derivation + author-time conflict)** (`tests/runtime/test_worker_placement.py` + `tests/core/config/test_pipeline_schema.py`): `resolve_required_label` returns the unique label / `None` when nothing is labeled / dedupes the same label across steps / ignores `sql` + component-less dbt + convention components / **raises the typed `ConflictingWorkerLabelsError`** on ≥2 distinct labels; `load_pipeline` rejects a conflicting-label pipeline at author time (typed error, `.file` set, both labels named); a single-label pipeline loads cleanly.
> - **Unit (scheduler stamps via resolver)** (`tests/runtime/test_worker_placement.py`): `run_due_once` with a `resolve_label` resolver stamps the pipeline's component label onto the scheduled job; `resolve_label=None` (default) leaves it unlabeled (existing scheduler tests unchanged); the resolver receives the pipeline name; a **raising** resolver skips only its own fire.
> - **CLI (`carve worker` / `carve serve --label`)** (`tests/cli/commands/test_worker_command.py`, `test_serve_command.py`): the flag sets `WorkerContext.label`, reaching `claim_next(worker_label=…)` + `register_worker(label=…)` (incl. `_with_worker_id` copying `label` on the id-rebind, forced via `worker_id=""`); serve's `resolve_worker_label` stamps `required_label` on scheduled fires **and** its **path-containment guard** returns `None` **without opening the file** for a `../`/absolute `schedule.pipeline` (proven by monkeypatching `load_pipeline` to fail-if-called against a valid planted `secret.toml`).
> - **End-to-end 2-worker placement** (`tests/runtime/test_worker_placement.py`): two in-process workers (one `--label`, one unlabeled) against one Postgres — a labeled job runs on the labeled worker only, an unlabeled job on either; deterministic (stubbed execute, sequential `run_once`, no subprocess/sleep).
> - **Migration 0013** (`tests/migrations/test_migrations.py`): up adds `jobs.required_label` **+ `jobs_archive`** (parity, **no** new index); downgrade drops it from **both**, restoring the 0012 schema; the archiver's `INSERT INTO jobs_archive SELECT *` round-trip is re-verified at head.
>
> The bullets below remain the spec's full-runtime test target (the **scheduler / schedule-mutation / pause-origin**, the **heartbeat / reaper**, the **archiver**, and now the **serve-supervisor / worker-pool crash-recovery** bullets are **SHIPPED** by the tests above — realized in-process; only the deferred FastAPI/leader-election supervisor bullets remain unbuilt):

- **Unit (scheduler):** cron `*/5 * * * *` fires at expected times under a controlled `Clock`; missed ticks (clock jumps forward by 20 minutes) produce one fire, not four — **SHIPPED**
- **Unit (schedule source of truth):** the scheduler fires from the `schedules` table row, not from `pipelines/<name>.toml`; a paused row is skipped by `list_due`; mutating a `[seed_schedule]` block (without `carve schedule reseed`) does not change which ticks fire — **SHIPPED** (the scheduler reads only `Schedules.list_due`; paused-row skip is tested)
- **Integration (schedule mutation audited):** `carve schedule pause`/`resume`/`set-cron` updates the row, takes effect within one scheduler loop interval, emits the matching `schedule.*` event, and appends a `schedule_changes` row with `before`/`after`/`actor_token_id`/`source` — no deploy/reconcile involved — **SHIPPED** (the `schedule.*` *emit* rode the no-op `_emit` seam; the durable `events` row is now **SHIPPED** with the events slice — migration 0010 + the injected `EventEmitter`)
- **Unit (pause origin gate):** `auto_pause_recovery` sets `paused_by='recovery'` on an active row but leaves a `paused_by='user'` row untouched; `auto_resume_recovery` resumes a `paused_by='recovery'` row but is suppressed when a user paused it in the interim; the `ck_schedules_pause_origin` CHECK rejects a paused row with NULL `paused_by` (and an active row with a non-NULL one) — **PARTIAL**: the **CHECK** half is SHIPPED + tested (ORM + raw-SQL); the `auto_pause_recovery`/`auto_resume_recovery` mutator half is **DEFERRED** (the recovery slice — the column value + CHECK origin ship, the mutators don't)
- **Unit (job_queue dedup):** two consecutive `enqueue_scheduled` for the same pipeline+scheduled_for: the second raises `QueuedJobAlreadyExists`
- **Unit (job_queue manual upsert):** `enqueue_manual` on a pipeline with an existing queued job updates it; the returned job_id matches the existing job's id
- **Unit (optimistic claim):** spawn 10 concurrent `claim_next` calls against 1 queued job; exactly one returns a job, nine return None
- **Unit (heartbeat):** a heartbeat loop running against a controlled clock writes `heartbeat_at` every interval; cancellation stops writes promptly — **SHIPPED** (under a `FakeClock`; a transient beat failure is survived; `stop` is idempotent)
- **Unit (reaper):** synthetic job with `heartbeat_at = now() - 70s` is reclaimed; `heartbeat_at = now() - 30s` is left alone — **SHIPPED** (via the atomic `reclaim_stale`; the orphaned in-flight run is failed; the ownership guard is the zombie-no-stomp companion)
- **Unit (archiver):** 100 completed jobs older than the window are archived; row counts match; deletion succeeds; verification failure halts the batch atomically — **SHIPPED**
- **Integration (serve lifecycle):** `carve serve --workers 2` starts cleanly against an empty Postgres; SIGTERM produces graceful shutdown; worker rows are removed from the table — **SHIPPED** (realized in-process: `_serve(..., worker_ctx=…, workers=2)` runs the pool + 3 loops and stops all on shutdown; the drain waits for the in-flight job then unregisters — `test_serve_runs_worker_pool_alongside_loops_and_stops_on_shutdown` + the graceful-drain pool test)
- **Integration (worker crash recovery):** spawn `carve worker`, queue a job, `kill -9` the worker mid-execution; reaper reclaims within 90s (60s threshold + 30s loop); next worker runs the pipeline successfully — **SHIPPED** (the deterministic in-process collapse of the `kill -9` ride: `force`-cancelling a mid-run worker leaves the in-flight job non-terminal for the already-shipped reaper — `test_second_signal_skips_grace_and_cancels`; no real subprocess)
- **Integration (concurrent claims):** spawn 5 worker processes against the same Postgres, queue 50 jobs; all 50 run, none twice — **SHIPPED** (deterministic in-process: N `worker_loop` coroutines with unique `:taskN` ids drain M queued jobs, each reaching a terminal state exactly once — `test_pool_drains_every_job_exactly_once`)
- **Integration (manual trigger dedup):** queue 50 manual triggers in rapid succession for one pipeline; database shows 1 running + 1 queued throughout; 50 client requests return 2 distinct job_ids (one for each row, with the 2nd–50th all returning the queued one)
- **Integration (scheduled while queued):** scheduler fires while a queued job exists for the same pipeline; emits `schedule.skipped`; does not insert a duplicate
- **Integration (long-running job + reaper):** a job whose execution legitimately exceeds 60s but maintains heartbeats is not reclaimed; verifies that the reaper's threshold isn't too aggressive
- **Integration (archiver verify-then-delete):** inject a synthetic failure between insert and delete; archive table has the rows, active table still has them, no data loss — **SHIPPED** (under the shipped single-transaction design the asserted bar is *no data loss / active table intact*; the injected failure rolls the INSERT back too — see §Archiver)
- **Unit (worker placement — claim label matrix):** a labeled worker (`worker_label="X"`) claims a matching labeled job (`required_label="X"`) **and** unlabeled jobs; an unlabeled worker claims **only** unlabeled jobs (SQL-NULL: `required_label = NULL` is never true); a labeled job **stays `queued`** for a non-matching/unlabeled worker until a matching worker claims it — **SHIPPED** (worker-placement slice; backfilled — no bullet existed)
- **Unit (worker placement — derivation, author-time conflict, scheduler stamp, CLI, migration):** `resolve_required_label` reduces the pipeline's referenced components' `worker_label`s to one (0→NULL, 1→that, ≥2→typed `ConflictingWorkerLabelsError`), and `load_pipeline` rejects a conflicting-label pipeline at author time; the scheduler `resolve_label` resolver stamps `required_label` (default `None` → unlabeled, existing scheduler tests unchanged); `carve worker --label`/`carve serve --label` reach the claim (+ serve's path-containment guard); migration 0013 up/down on both `jobs` + `jobs_archive` — **SHIPPED** (worker-placement slice; backfilled — no bullet existed)

## Acceptance

> **Updated during implementation (2026-06-26):** the criteria below are the **full-runtime** acceptance target and remain the spec's bar. The **lean first slice** satisfies the slice-scoped subset: **`carve worker --once`** (and the loop) against a freshly-initialized Postgres claims a queued job, creates its `runs` row, transitions it to `running`, executes the pipeline, and persists `step_runs` + the terminal `runs`/`jobs` rows; the **partial unique indexes structurally prevent more than one queued and one running job per pipeline**; **50 concurrent manual triggers** produce 1 running + 1 queued (the `enqueue_manual` upsert), not 50 queued; a worker failure after claim never orphans the job (it is marked `failed`).
>
> **Updated during implementation (2026-06-26, scheduler slice):** the **scheduler slice** satisfies the next subset: the **scheduler treats the `schedules` table as the source of truth** and fires due rows onto the queue (within one ≤30s loop interval), advancing `next_fires_at` so each window fires once; **`carve schedule pause/resume/set-cron` changes firing within one loop interval without a deploy/reconcile, and every such change appends a `schedule_changes` audit row**; **a human's explicit pause is structurally protected** — the `ck_schedules_pause_origin` CHECK (with the `paused_by IS NOT NULL` guard) ships, so the recovery slice's `auto_pause_recovery`/`auto_resume_recovery` will land against a complete origin column (the *mutators themselves* — and thus the "recovery never overrides a `user` pause" runtime rule — are **DEFERRED** with the recovery slice). **Still DEFERRED** with their slices: reaper stale-claim detection, the archiver verify-then-delete, the full `carve serve` supervisor + graceful drain, and crash recovery.
>
> **Updated during implementation (2026-06-26, heartbeat+reaper slice):** the **heartbeat+reaper slice** satisfies the next subset and **completes the queue's crash-recovery story**: the worker stamps `heartbeat_at` on a best-effort interval while holding a job, and the **reaper reclaims a job whose heartbeat goes stale** (`< now - 60s`) back to `queued`, failing its orphaned in-flight Run (`worker_crashed_or_unreachable`) — so **a crashed worker (`kill -9`) doesn't lose data; the next worker re-runs from scratch** (the "crash recovery" acceptance bullet below is now satisfied at the unit level — the full multi-process `carve worker` + `kill -9` integration ride lands with the worker-pool slice). The **ownership guard** (`expected_worker_id` on the three claim-owning writes) makes a returning zombie worker's write a no-op, so a reclaimed-then-resurrected worker can't double-run or stomp the new owner. `carve serve` now hosts **scheduler + reaper** as two co-running loops (so "reaper reclaims stale claims" in the headline bullet is live). **Still DEFERRED** with their slices: the archiver verify-then-delete, the full `carve serve` supervisor (`--workers N` / FastAPI / leader-election / graceful drain), and the recovery auto-pause/auto-resume mutators.
>
> **Updated during implementation (events slice, 2026-06-29):** the **events slice** satisfies the events subset (the spec had **no** events-specific Acceptance bar — events were deferred without their own bullets). Slice-scoped Acceptance, now met: a **durable `events` row is written for each in-scope transition** that has a transition point today — `schedule.skipped|paused|resumed|changed|seeded`, `job.queued|claimed|reclaimed`, `run.started|succeeded|failed`, `step.started|completed|failed`, `worker.registered|unregistered` (`archive.batch_completed` / `schedule.reseeded` stay deferred with their slices); each previously-no-op `_emit` seam **persists when an emitter is injected** and stays a **silent no-op without one** (back-compat); an `on_run_failed`-subscribed hook **fires on a runtime `run.failed` transition**, gated/fail-closed at `DEPLOY`, **post-event** (a raising hook is logged, the run stays terminal-failed); **migration 0010** creates `events` + the partial `ix_events_unprocessed` and downgrades cleanly to the 0009 schema. **Still DEFERRED** with their slices: archiver verify-then-delete, the full `carve serve` supervisor, the recovery auto-pause/auto-resume mutators, and the `stale_threshold_s` floor validation (no `runtime.toml` surface landed).
>
> **Updated during implementation (archiver slice, 2026-06-29):** the **archiver slice** satisfies the archiver subset of the bars below: the archiver, on its own configurable loop, **moves terminal rows older than each table's window** from `jobs`/`runs`/`logs`/`step_runs` into their `*_archive` tables; it **never deletes from an active table until the archive insert is verified** (count match) — an `ArchiveVerificationFailed` **halts the batch atomically with no data loss** (so the "archiver moves old rows" clause of the headline `carve serve` bullet and the standalone "never deletes … until verified" bullet are now live); `carve serve` hosts the archiver as a **3rd co-running loop** (`--no-archiver`/`--archive-interval`); each batch emits a durable `archive.batch_completed` when an emitter is injected. **Still DEFERRED** with their slices: the full `carve serve` supervisor (`--workers N` / FastAPI / leader-election / graceful drain), the recovery auto-pause/auto-resume mutators, observability, and the `stale_threshold_s` floor validation (its config surface still hasn't landed — `[runtime.archive]` is windows + interval only).

> **Updated during implementation (worker-pool slice, 2026-06-29):** the **worker-pool slice** satisfies the supervisor-drain subset and turns the remaining headline bars live: **`carve serve --workers 1`** (default; `--workers N` for more) now runs the full live loop — the scheduler fires due jobs, the **worker pool** claims and runs them, the reaper reclaims stale claims, and the archiver moves old rows; **`carve serve` graceful shutdown completes within the configured grace period** (`--drain-timeout`/`--grace-period`, default 300s) **or escalates cleanly** (a 2nd signal `force`-cancels the stragglers); a **crashed/cancelled worker doesn't lose data** — the force-cancelled in-flight job is left `running`/stale for the reaper (the `CancelledError`→`finally`→reaper path), and the inherited `expected_worker_id` guard blocks a zombie-stomp. Realized as **deterministic in-process** tests (the spec's "spawn N processes / `kill -9`" cases collapse to N `worker_loop` coroutines sharing one Postgres + the `FakeClock`/injected-registry seams — no real subprocesses). **Still DEFERRED** with their slices: the rest of the `carve serve` supervisor (FastAPI / `--port`/`--host` / leader-election), worker-placement labeling (`--label`), the recovery auto-pause/auto-resume mutators, and observability (`carve metrics`).

> **Shipped during implementation (worker-placement slice, 2026-07-01):** the **worker-placement slice** satisfies the placement subset — **un-deferring** the "worker-placement labeling (`--label`)" item the worker-pool callout just above still listed as DEFERRED (the spec had **no** worker-placement-specific Acceptance bar; the design lived only in the one §"Worker placement & labeling" paragraph). Slice-scoped Acceptance, now met: a **labeled pipeline** (a referenced component sets `worker_label`) enqueues a job whose derived `required_label` is claimed **only** by a matching `carve worker --label` / `carve serve --label` worker and left `queued` by a non-matching/unlabeled one; an **unlabeled** pipeline runs on **any** worker (the flat pool, byte-identical to before); a **≥2-distinct-label** pipeline is **rejected at `load_pipeline`** author-time (typed `ConflictingWorkerLabelsError`, never enqueued); `worker_label`/label unset everywhere ⇒ the flat any-worker-any-job pool (back-compat). This is the **7th and final Increment-4 runtime slice** — **runtime's Increment-4 scope is now complete**; the only runtime behavior still deferred in this spec is the recovery **`auto_pause_recovery`/`auto_resume_recovery`** mutators (Increment 6, [recovery](./recovery.md)).

- `carve serve --workers 1` against a freshly-initialized Postgres runs end-to-end: scheduler fires due jobs, worker claims and runs them, reaper reclaims stale claims, archiver moves old rows
- The scheduler treats the `schedules` table as the source of truth; `carve schedule pause/resume/set-cron` changes firing within one loop interval without a deploy or reconcile, and every such change appends a `schedule_changes` audit row
- A human's explicit pause always wins over recovery's automatic one: recovery's `auto_pause_recovery`/`auto_resume_recovery` never override or auto-resume a `paused_by='user'` schedule
- Per ARCHITECTURE §13.1 budgets:
  - Scheduler latency: jobs fire within 30 seconds of their cron time
  - Run startup overhead: under 10 seconds from claim to first step execution
  - Reaper detects stale workers within 60 seconds of last heartbeat
- The partial unique indexes structurally prevent more than one queued and one running job per pipeline
- A crashed worker (`kill -9`) doesn't lose data; the reaper reclaims its job and the next worker runs from scratch
- A **labeled pipeline** (a referenced component sets `worker_label`) enqueues a job with the derived `required_label`, claimed **only** by a worker started with a matching `carve worker --label` / `carve serve --label`; an **unlabeled** pipeline runs on **any** worker (the flat pool, back-compat); a pipeline whose components require **≥2 distinct labels** is **rejected at `load_pipeline`** author-time with a typed `ConflictingWorkerLabelsError` (never enqueued)
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

- **Heartbeat interval and stale threshold values.** *Implementation default.* 10s heartbeat, 60s stale threshold. Tunable in `runtime.toml`. Default chosen so 5 consecutive missed beats trigger reclaim — tolerant enough to handle DB hiccups, aggressive enough to recover from real crashes within a minute. **Shipped this slice as the hardcoded defaults** (`DEFAULT_HEARTBEAT_INTERVAL_S` / `DEFAULT_STALE_THRESHOLD_S`); `stale_threshold_s` has **no config/CLI surface yet** (safe — not user-tunable, so not abusable). **Forward item (security reviewer, non-blocking):** when `runtime.toml` makes `stale_threshold_s` tunable, add a **floor validation** so it cannot be set below the missed-beat margin (≥ a few heartbeat intervals) — a too-small threshold would reap live workers mid-run. Pick it up when the config surface lands. **(Archiver slice, 2026-06-29 — still deferred):** the first `runtime.toml` surface landed as `[runtime.archive]` (windows + interval) only; it does **not** expose `stale_threshold_s`, which stays the hardcoded `DEFAULT_STALE_THRESHOLD_S` (safe — not user-tunable, so not abusable). The floor validation stays owed until a `stale_threshold_s` surface lands.
- **Owed (non-blocking, archiver slice — security MINOR): clamp the `carve serve` interval flags.** `--archive-interval` (and the pre-existing `--interval`/`--reaper-interval`) read the **unbounded** Typer option directly, bypassing the `ge=1`-validated `ArchiveConfig.interval_s` field, so `carve serve --archive-interval 0` (or a negative) **busy-loops** the loop. Operator-controlled, so MINOR; clamp all three serve interval flags to a positive floor in a later pass.
- **Worker count default for `carve serve`.** *Strategy-already-resolved.* Default 1 per the initial positioning decision (single worker, serial). Users scale with `--workers N` or `carve worker` processes.
- **Owed (non-blocking, worker-pool slice).** Four forward items flagged during the worker-pool build, none blocking: **(a)** `_serve._on_signal`'s 1st/2nd-signal mapping is exercised but only asserted at the `run_worker_pool` unit level, not driven *through* `_serve`'s installed handler — add a thin test-seam to assert the handler's `shutdown`-then-`force` transition later. **(b)** `carve serve --workers 0` silently runs **no** pool (the `workers >= 1` gate) while `carve worker` rejects `< 1` — arguably correct (a `serve --workers 0` coordinator/scheduler-only node is a documented scale-out pattern), but `serve` should validate `--workers >= 0` (reject **negatives**, which today also silently no-op the pool) and **document "0 = coordinator / no local pool."** **(c)** a `to_thread`-blocked worker (a step stuck in `asyncio.to_thread`) can delay the final pool join **past** the grace period — an inherent `asyncio.to_thread` limitation (the same caveat as the sql-step timeout), not fixable by cancellation alone. **(d)** unbounded `--workers N` is **operator-controlled** — there is no backpressure (the `max_queue_depth` setting stays deferred, below); a too-large pool can overwhelm the queue/warehouse, which is the operator's signal to tune it down.
- **Archive table partitioning.** *Implementation default.* No partitioning in OSS (a single-team install's archive grows slowly enough that partitioning is unjustified complexity). Hosted partitions by month for query performance — that's a hosted-side concern, out of scope for this spec.
- **Backpressure when the queue is overloaded.** *Implementation default.* No special handling initially; if the queue grows unbounded, that's the user's signal to add workers. Future enhancement: a `max_queue_depth` setting that rejects new triggers above the threshold. Defer until someone hits it.
- **Behavior when Postgres becomes unreachable mid-run.** *Implementation default.* Workers attempt to reconnect with backoff; heartbeats stop; reaper reclaims the job after threshold. The in-flight subprocess (dlt/dbt) keeps running and may complete its destination writes — those are idempotent enough (dlt's incremental state, dbt's run_results) that the eventual rerun won't double-write. Documented in `docs/runtime-troubleshooting.md`.
- **Owed (non-blocking, scheduler slice): convert `Schedules.seed`/`set_cron`'s create path from check-then-insert to `ON CONFLICT`.** *Forward item — flagged by the python reviewer, not a change in this slice.* `seed`/`set_cron` row-lock then insert-or-update (`_get_locked` → `add`), which is correct for this slice's **single-actor CLI** create path. But the FUTURE PIPELINES reconciler will call `seed` **concurrently** at registration, so before/with the reconciler slice the create path should convert to `INSERT … ON CONFLICT (pipeline, tenant_id) DO UPDATE` — matching the shipped `JobQueue.enqueue_scheduled`/`enqueue_manual` ON-CONFLICT precedent — to close the concurrent-seed race. Pick it up when the reconciler-seed lands.
- **Ownership of `schedule_changes` and the `carve schedule` live-mutation surface.** ✅ **CONFIRMED + IMPLEMENTED (2026-06-26, scheduler slice).** This slice ships the split the engineer flagged, resolving the open question: **this spec (runtime) owns** the live `schedules` table + `schedule_changes` (migration `0009_runtime_schedules`) + the `carve schedule list/show/pause/resume/set-cron` mutation surface (`cli/commands/schedule/`) + the `Schedules` repo's mutators; **PIPELINES/spec-08 keeps** the `[seed_schedule]` reconciler-seed + `carve schedule reseed` (a deferred stub here that exits non-zero and points at the reconciler). Note: `schedule_changes` shipped in migration **0009** (the scheduler slice), not 0008 (the original "migration 0008" guess predated the slice split). The split is consistent with 08 §"Out of scope" delegating the live table + mutation surface to this spec; ARCHITECTURE §9.3's table ordering is descriptive, not an ownership claim.
