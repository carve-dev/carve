# Carve — Use Cases

> Living document. Captures end-to-end walkthroughs of how real users (analysts, analytics engineers, data engineers, ops) interact with Carve. Each walkthrough has been pressure-tested in design discussion and surfaces concrete decisions about CLI, REST, MCP, file layout, runtime, and operational model.
>
> **How to use this doc.** When changing a spec under `specs/v0.1/`, `specs/pillar-*/`, `PRD.md`, or `ARCHITECTURE.md`, validate the change against the walkthroughs below. If a change breaks a walkthrough, either revise the change or update the walkthrough explicitly (with rationale).

## Template

Each use case follows this shape:

- **Persona** — who's driving
- **Goal** — what they want to accomplish
- **Where it happens** — laptop, central server, chat tool, CI
- **Pre-conditions** — what must already be true
- **Walkthrough** — numbered steps, naming the actor (analyst, orchestrator, EL specialist, CI, prod scheduler) at each turn
- **Design decisions surfaced** — concrete things the spec must support
- **Open questions** — decisions deferred or still fuzzy

## Cross-cutting model

Two operational shapes underlie every use case below:

- **Developer install (laptop).** `pip install carve` + project repo cloned. `.env` holds only **dev** credentials. Carve is **Postgres-only from day one** (SQLite was retired in spec 01): `carve init` bundles a one-command docker-compose Postgres, or you point at managed Postgres via `--external-postgres`. The same install authors (`plan`/`build`/`deploy`) and runs a local `carve serve` with scheduler/workers. CLI/MCP can drive a local `carve serve` OR run plan/build in-process.
- **Central server install (one per organization).** Same package, deployed as a long-running service. `.env` holds only **prod** credentials. Managed Postgres. `carve serve` runs 24/7 with scheduler + workers. Exposes REST + MCP on an internal URL.

**Targets are a soft contract; credential presence is what makes a target actually runnable on a given install.** A laptop physically cannot touch prod because it has no prod credentials. Code crosses the dev/prod gap via git PR → CI-driven deploy to the central server.

**Credentials are referenced, never owned.** Carve writes `${VAR}` references into `.dlt/secrets.toml`, `profiles.yml`, and `connections.toml`. Actual values live in `.env` (or env vars injected by the host). Carve never persists secrets in its state store.

**Auth.** Each install has a `tokens` table with three roles: `admin`, `member`, `read-only`. The bootstrap token from `carve init` is `admin`. Admins mint additional tokens via `carve tokens create`; developers store per-server credentials in `~/.carve/credentials`. Every action attributes back to a `triggered_by_token_id`.

**Carve follows dlt's defaults unless explicitly overridden.** When generating EL artifacts we mirror dlt's idioms — `--refresh` modes, schema contract defaults, source scaffolding, exception classification — rather than invent Carve-specific equivalents. Any deviation from dlt's recommended behavior must be named explicitly in the use case where it's introduced. Three concrete consequences:
- **Curated source library is a registry over dlt's verified sources**, not a fork. `src/carve/sources/<name>/` is a small Carve-side metadata file (env vars expected, connection-validation glue, optional convention hints); the actual dlt source code lands via `dlt init <source>` wrapped by `carve connect`. dlt's verified-sources upstream bug fixes flow through on `pip install -U dlt`.
- **`carve run --refresh <mode>` mirrors dlt's modes** (`drop_sources`, `drop_resources`, `drop_data`) one-to-one. Carve adds confirmation prompts on `drop_data` against prod, but does not rename or collapse the modes.
- **The recovery agent classifies failures on dlt's exception hierarchy** (`DataValidationError` for schema-contract violations, `LoadClientJobFailed` for terminal load failures, `DatabaseUndefinedRelation` for missing relations, etc.) rather than a Carve-invented taxonomy. It unwraps the top-level `PipelineStepFailed` to its `.exception` and matches on dlt's terminal-vs-transient base classes (`DestinationTerminalException` / `DestinationTransientException`). Auth failures have no dedicated dlt class — they're matched on the source-side HTTP 401/403 inside `.exception`.

---

## Use Case 1 — Analyst ingests Salesforce into Snowflake

**Persona.** Semi-technical analyst. Comfortable in a terminal, comfortable in a chat tool, not comfortable hand-writing dlt source code or dbt YAML.

**Goal.** Pull Salesforce Accounts, Contacts, and Opportunities into Snowflake so downstream dbt models can reference them.

**Where it happens.** Analyst's laptop. Authoring is driven primarily through Claude Desktop (or Claude Code) with Carve's MCP server configured; the analyst falls back to the CLI for credential-handling steps that must not pass through chat.

**Pre-conditions.**
- Project repo cloned to laptop with `carve.toml`, `el/`, `pipelines/`, `carve/` already scaffolded (a teammate ran `carve init` previously, or this is the first use case after init)
- Snowflake `dev` target connection exists in `connections.toml` with `${VAR}` references; corresponding env vars exist in laptop's `.env`
- Snowflake `dev` is already verified (or this story is preceded by a `carve connect snowflake --target dev` step — see open questions)
- `ANTHROPIC_API_KEY` is set, or `carve auth login` has produced an OAuth token

### Walkthrough

1. **Analyst (chat):** "I want to pull Salesforce Accounts, Contacts, and Opportunities into Snowflake."
2. **Orchestrator (via MCP):** Classifies goal → new EL pipeline (source: salesforce, destination: snowflake/dev). Calls the `verify_dlt_source(salesforce, dev)` skill before any planning.
3. **`verify_dlt_source` skill:** Reads `.dlt/secrets.toml` and `.env`. Finds no `[sources.salesforce]` section (or finds one with placeholder values). Returns `unverified, reason: "credentials missing"`.
4. **Orchestrator (chat reply):** "Salesforce isn't connected yet. I'll set up the credential placeholders. Run `carve connect salesforce --target dev` in your terminal — that will add the entries to `.dlt/secrets.toml`, tell you which env vars to populate, and test the connection once you've set them."
5. **Analyst (terminal):** `carve connect salesforce --target dev`
6. **`carve connect` command:**
   - Looks up `salesforce` in Carve's curated source registry (`src/carve/sources/salesforce/`) → it's a known source backed by dlt's verified-sources `salesforce` package.
   - Wraps `dlt init salesforce snowflake` under the hood. This scaffolds the dlt source into `el/salesforce/` (Carve's convention), including the `__init__.py` with `@dlt.source` / `@dlt.resource` definitions, `requirements.txt`, and `.dlt/secrets.toml.example` / `.dlt/config.toml.example` templates.
   - Merges the placeholders into `.dlt/secrets.toml` as `${SALESFORCE_CLIENT_ID}`, etc. references; adds matching `SALESFORCE_*=` placeholders to `.env.example`.
   - Prints: "Set these in `.env`: `SALESFORCE_CLIENT_ID`, `SALESFORCE_CLIENT_SECRET`, `SALESFORCE_SUBDOMAIN`. Then run `carve connect salesforce --target dev` again to verify."
7. **Analyst:** Pastes credentials into `.env`, re-runs `carve connect salesforce --target dev`.
8. **`carve connect` command:** Resolves env vars, attempts a Salesforce auth call. Succeeds. Prints "✓ salesforce@dev verified."
9. **Analyst (chat):** "Done. Now plan the ingest."
10. **Orchestrator:** Calls `verify_dlt_source(salesforce, dev)` again — passes. Calls `verify_dlt_destination(snowflake, dev)` — passes. Calls `salesforce_list_objects()` (now possible since we have creds) to confirm the requested objects exist.
11. **Orchestrator → EL specialist:** Pre-scoped context includes the verified connection metadata, the matched curated source (`src/carve/sources/salesforce/`), inferred conventions from `carve/conventions.md` (e.g., team uses `raw_*` schema prefix), the existing dbt sources file, the requested object list.
12. **EL specialist:** Generates Plan with file diffs:
    - `el/salesforce/__init__.py` (customized from curated source)
    - `el/salesforce/requirements.txt`
    - `pipelines/salesforce.toml` (single-step dlt pipeline, dev target by default, stub `[schedule]` block)
    - `sources.yml` patch adding `salesforce` source with three tables (if dbt is wired up)
13. **Plan returned to chat.** Analyst sees file diffs, cost estimate, the inferred schema (`raw_salesforce`).
14. **Analyst (chat):** "Use `raw_sfdc` schema instead, that's what we use." → triggers `carve plan --refine`.
15. **Orchestrator:** Produces child plan with updated schema.
16. **Analyst:** "Looks good, build it."
17. **Orchestrator:** Invokes `carve build <plan_id>` — writes files into the repo working copy.
18. **Analyst:** "Run it against dev."
19. **Orchestrator:** Invokes `carve run salesforce --target dev`. Worker (local or in-process) shells out to `dlt pipeline run salesforce`. Rows land in `dev_db.raw_sfdc.{accounts, contacts, opportunities}`.
20. **Analyst:** Inspects rows directly in Snowflake or via `carve runs show <run_id>` for log/metrics output.
21. **Analyst:** "Ship it."
22. **Orchestrator:** Invokes `carve deploy salesforce`. Creates a feature branch, commits the new files + `sources.yml` patch + `.env.example` update, pushes, opens a PR via GitHub MCP.
23. **Human reviewer (could be analyst, could be data engineer):** Reviews PR. CI runs `dlt pipeline check`, `dbt parse`, lints. PR is merged to `main`.
24. **CI workflow on merge to main:** Builds and deploys the new code to the central `carve serve` (e.g., `kubectl rollout restart`, `docker compose pull && up -d`, or `systemctl restart carve`). Recommended template ships with `carve init`.
25. **Prod `carve serve`:** Boots with the updated code. Scheduler reads `pipelines/salesforce.toml`, finds the schedule (default daily or whatever the analyst set), inserts a row into the schedules table.
26. **Prod scheduler (next cron tick):** Inserts a job. A worker claims it, runs `dlt pipeline run salesforce --target prod` using prod's env vars. Rows land in `prod_db.raw_sfdc.{accounts, contacts, opportunities}`.
27. **Analyst (later, from laptop, optional):** `CARVE_SERVER_URL=https://carve.internal.co carve runs --pipeline salesforce` to confirm the first prod run succeeded. Or just gets a Slack webhook notification on `run.succeeded`.

### Design decisions surfaced

- `carve connect <integration>` is a real command. Scaffolds credential refs, instructs user, verifies on retry.
- Credential placeholders go into `.dlt/secrets.toml` and `.env.example`; values into `.env`. Carve never owns the secret material.
- `verify_dlt_source` / `verify_dlt_destination` are **skills** (not specialists). The orchestrator calls them before planning; failure produces a guidance response, not a plan.
- The orchestrator is a **gatekeeper** in addition to a planner — it can refuse to plan and instead instruct the user to complete pre-flight steps.
- Curated source library (`src/carve/sources/<name>/`) is a Carve-side **registry** that maps source names to dlt's verified-sources packages, declares the env var names a source expects, and provides Carve-specific connection-validation glue. The actual dlt source code is scaffolded by wrapping `dlt init` — Carve does not maintain a fork of dlt's source code.
- **Hint style follows dlt's verified-sources defaults: mostly inference, with explicit hints on critical columns** (primary key, cursor column, anything the agent has a structural reason to pin). The EL specialist can add hints during refinement when the analyst needs stricter contracts on specific columns; otherwise dlt's schema inference does the work.
- Once a source is verified, the orchestrator can call source-specific introspection skills (`salesforce_list_objects`, etc.) to inform planning. These are exposed as part of each curated source.
- No credentials ever pass through the LLM context. The flow is intentionally split between chat (instructions) and terminal (credential entry + verification).
- `carve deploy` opens a PR; it does **not** push code to the central server. CI on merge handles the rollout.
- `--target` defaults to `dev` for laptop-driven actions. Prod runs are always either scheduled or explicitly invoked against the central server (`CARVE_SERVER_URL=...`).
- The analyst can view prod state from their laptop by pointing the CLI/MCP at the prod URL with their personal token.

### Resolved decisions

- **OAuth orchestration is deferred to post-v0.1**, matching dlt's stance — dlt doesn't ship a unified OAuth UX either; per-source auth tokens are obtained out-of-band and dropped into `.dlt/secrets.toml`. Carve documents the manual flow per source. Post-v0.1 may add a side-channel browser flow as a Carve value-add where dlt doesn't.
- **`carve init` populates `carve/conventions.md`** with sensible defaults (e.g., `raw_<source>` schema prefix, lower_snake_case columns, merge disposition default). The agent reads from the file — single source of truth, no hardcoded defaults in agent code.
- **`carve env set / list / unset` ship in v0.1** as an interactive surface over `.env`. `set` accepts a value via masked stdin; `list` shows names only (never values); `unset` removes. MCP-equivalent for chat-driven flows.
- **Plan output shows exact LLM cost** (we know it precisely) and **estimated runtime time** (first run / subsequent runs) — no dollar estimate for warehouse compute. Time is what the analyst actually wants to know; warehouse-cost estimation is too variable to deliver responsibly.

### Open questions

- **Non-curated sources (custom dlt code).** When the analyst writes their own dlt source instead of using a curated one, where does `carve connect` discover the required env-var names? Likely from the dlt source's `@dlt.source` config introspection. Worth confirming with a smoke test.

---

---

## Use Case 2 — Deploy a locally-tested pipeline to prod with a 15-minute schedule

**Persona.** Same analyst from UC1. Built the Salesforce ingest locally, ran it against dev, rows look right.

**Goal.** Promote the pipeline to prod and have it run every 15 minutes from the central server.

**Where it happens.**
- Setting the schedule → analyst's laptop (declarative, via plan/build/deploy)
- Reviewing the PR → git provider
- Rolling out → CI workflow on PR merge
- Picking up the schedule → prod `carve serve` reconciles `pipelines/*.toml` against the `schedules` table
- Running → prod scheduler fires; prod worker executes against prod target
- Observing / adjusting later → analyst's laptop CLI/MCP pointed at the prod server with their personal token

**Pre-conditions.**
- UC1 completed locally: `el/salesforce/`, `pipelines/salesforce.toml` (no `[schedule]` block or a `paused = true` placeholder), and the dbt source patch exist on a feature branch
- Local `carve run salesforce --target dev` has been verified
- Central `carve serve` is deployed at `https://carve.internal.co` and CI rollout pipeline is configured
- `connections.toml` has a `[snowflake.prod]` block referencing prod env vars; prod server has those env vars set
- The analyst has a `member` token for the prod server (issued previously by an admin — see UC4 for issuance)

### Walkthrough

**Setting the initial schedule (laptop)**

1. **Analyst (chat):** "Set this pipeline to run every 15 minutes in prod."
2. **Orchestrator:** Classifies → pipeline modification (schedule only). Routes through plan/build for the audit trail rather than mutating runtime state directly (see open question on this choice).
3. **Orchestrator → runtime specialist:** Pre-scoped context includes current `pipelines/salesforce.toml` content, the requested cadence, and the team's timezone convention from `carve/conventions.md` or `runtime.toml`.
4. **Runtime specialist:** Generates a Plan with a single file diff — `pipelines/salesforce.toml` gains:
   ```toml
   [schedule]
   cron = "*/15 * * * *"
   timezone = "UTC"
   target = "prod"
   paused = false
   ```
5. **Plan returned to chat.** Analyst sees the one-file diff and a human-readable cron summary ("every 15 minutes, UTC").
6. **Analyst:** "Build and deploy."
7. **Orchestrator:** `carve build <plan_id>` writes the file. `carve deploy salesforce` either amends the still-open UC1 PR (if it hasn't merged) or opens a new PR (if UC1 has already shipped). See open question.

**PR review and rollout (collaborative)**

8. **Reviewer:** Reviews PR diff. CI runs `dlt pipeline check`, `dbt parse`, lints, plus a `carve schedule validate` check that the cron expression parses cleanly. PR is merged.
9. **CI on merge to main:** Triggers the rollout job (recommended template ships with `carve init`). For K8s: `kubectl rollout restart deployment/carve-serve`. For docker-compose: `docker compose pull && docker compose up -d`.

**Prod picks it up**

10. **Prod `carve serve` boots (and runs a reconciler loop):** The schedule reconciler scans `pipelines/*.toml`. For each pipeline:
    - No row in `schedules` table → insert from TOML
    - Row exists, TOML differs, no active runtime override → update from TOML
    - Row exists, TOML differs, active runtime override → override holds for its TTL; TOML update is staged and takes effect when override expires (Option B precedence — see "Adjusting the schedule after deploy" below)
    - Row exists for a pipeline whose TOML has no `[schedule]` block → mark schedule as `removed`
    - TOML has `paused = true` → row created with status `paused-by-code` (visible in `carve schedule list` so the team can see intentionally-paused pipelines)
    
    Emits `schedule.reconciled` events with before/after for each change.
11. **Schedule row exists:** `schedules.salesforce.next_fires_at` is set to the next `*/15` boundary.

**First scheduled run**

12. **Prod scheduler (next 15-min tick):** Inserts a row into `jobs` with `pipeline=salesforce, target=prod, trigger=scheduled, scheduled_for=<tick>`.
13. **Prod worker:** Claims the job (ARCHITECTURE §4.3 optimistic claim), shells out to `dlt pipeline run salesforce`. dlt resolves credentials from prod env vars. Rows land in `prod_db.raw_sfdc.{accounts, contacts, opportunities}`.
14. **Run completes:** Emits `run.succeeded` event. Webhook (if configured) posts to Slack: "salesforce run #1234 succeeded in prod, 3 tables loaded".

**Analyst confirms (laptop)**

15. **Analyst:** From laptop, `CARVE_SERVER_URL=https://carve.internal.co carve schedule show salesforce` → cron, target, next_fires_at, last_fired_at, paused status, last 5 runs.
16. **Analyst (chat, MCP pointed at prod):** "Show me the last salesforce runs." MCP tool hits prod `/api/v1/runs?pipeline=salesforce&limit=10`. Chat renders the result.

### Adjusting the schedule after deploy

**Durable change (code path):**
- Edit `pipelines/salesforce.toml` locally (or via chat: "Change schedule to every 30 mins")
- `plan → build → deploy → PR merge → CI rollout → reconciler picks up`
- This is the "real" answer — code is source of truth

**Ad-hoc change (runtime path, audited):**
- `carve schedule pause salesforce` (against prod) → sets `schedules.paused = true`. Takes effect within 30 seconds. Reason captured.
- `carve schedule resume salesforce`
- `carve schedule override salesforce "*/30 * * * *" --reason "load test" --expires "+2h"` → runtime override visible alongside the TOML value. **Override survives reconciles until its TTL expires** (Option B precedence — kubectl-style). A deploy in the middle of an override's TTL doesn't wipe the override; once the TTL expires, the reconciler applies the current TOML value.

**Audit trail:** Every schedule mutation (code reconcile or runtime override) appends a row to a `schedule_changes` audit table with `actor_token_id`, `change_kind`, `before`, `after`, `reason`, `expires_at`. Visible via `carve schedule history <pipeline>`.

### Permissions

| Role | View | Pause / resume | Runtime override | Code-path change (PR) | Clear another's override |
|---|---|---|---|---|---|
| **admin** | yes | yes | yes (any TTL) | via PR | yes |
| **member** | yes | yes | yes (TTL ≤ configured cap, default 24h) | via PR | no |
| **read-only** | yes | no | no | n/a | no |

The member-override TTL cap lives in `runtime.toml` (`[scheduling] member_override_max_ttl = "24h"`). Repo-write access is independent of Carve roles — anyone who can open a PR can propose a code-path schedule change.

### Design decisions surfaced

- **Schedule is declared in `pipelines/<name>.toml`** (code is source of truth) and reconciled into the `schedules` table on prod server boot and on a periodic reconciler loop.
- **A reconciler loop runs inside `carve serve`** alongside the scheduler. Configurable interval (default 60s). Emits `schedule.reconciled` events.
- **Runtime overrides exist for ops scenarios** (pause, temporary cadence change) but are time-bound and audited.
- **A `schedule_changes` audit table** records every mutation with the actor token. New table, add to ARCHITECTURE §9.
- **CLI surface:** `carve schedule show / list / pause / resume / override / clear-override / history` — all available via REST + MCP.
- **CI runs `carve schedule validate`** on every PR to catch broken cron expressions before merge.
- **First scheduled run is on the next cron tick** after rollout, not immediate. A separate `carve run salesforce --target prod` (manual, member-allowed) lets the analyst trigger an immediate run to verify prod before waiting.
- **Timezone defaults to UTC** with per-schedule override; team default settable in `runtime.toml`.
- **Per-pipeline serialization (ARCHITECTURE §4.2)** means a 15-min schedule that occasionally takes longer than 15 mins won't pile up — at most one queued + one running per pipeline; missed ticks emit `schedule.skipped`.
- **Permissions matrix:** admin / member / read-only as defined; code-path changes require repo write, not a Carve role.

### Resolved decisions

- **Schedule changes go through plan/build/deploy/PR** even for one-line TOML diffs. Consistency + audit trail win over speed. Runtime overrides (with TTL) are the escape hatch for short-term needs.
- **TOML-vs-runtime-override precedence: Option B (kubectl-style).** Overrides survive reconciles until their TTL expires; TOML updates stage during an active override and take effect when it expires. A teammate's PR can't silently wipe your incident mitigation.
- **Member runtime-override TTL cap: 24h default**, configurable in `runtime.toml` (`[scheduling] member_override_max_ttl = "24h"`). Long enough for an overnight incident, short enough that "I'll fix it Monday" forces a PR.
- **`paused = true` in TOML creates a `schedules` row** with status `paused-by-code`. Visible in `carve schedule list`. Omitting `[schedule]` entirely creates no row.
- **`carve deploy` output + post-merge Slack** include "next scheduled run at HH:MM" so the analyst knows when to expect the first prod run. No magic `--run-now-after-merge` flag — manual `carve run salesforce --target prod` is the explicit verification path.

### Open questions

- **What if the UC1 PR hasn't been merged yet?** Amend the existing PR (cleaner git history but confuses reviewers mid-review) or open a second PR (more churn but each PR is one logical change)? Probably: amend if same logical work being iterated; new branch if UC1 already merged. Worth a CLI flag.
- **Multiple schedules per pipeline.** Some pipelines want "every hour business hours, every 4 hours at night." Not in v0.1; flag as deferred. Future shape: `[[schedule]]` table list.

---

## Use Case 3 — First run full-load, subsequent runs incremental

**Persona.** Same analyst from UC1/UC2. Pipeline built, scheduled, deployed. Now realizing that pulling all Salesforce records every 15 minutes is wasteful and slow.

**Goal.** Configure the Salesforce pipeline so the first run does a full historical pull and every subsequent run pulls only records changed since the last run. Each Salesforce object can have its own per-resource policy (most are incremental, a small reference object like `User` might be full-replace every time).

**Where it happens.**
- Configuring the incremental policy → laptop, via chat (plan/build/PR)
- First prod run → prod scheduler picks up the new code, fires a (long) full-load run
- Subsequent prod runs → prod scheduler, every 15 min, incremental
- Cursor state → stored in the destination warehouse (recommended), automatically isolated per target

**Pre-conditions.**
- UC1 + UC2 completed: pipeline exists in prod, scheduled `*/15 * * * *`, first scheduled prod runs have already happened (currently doing full loads every time)
- Analyst has a `member` token for prod

### Walkthrough

**Specifying the policy (laptop)**

1. **Analyst (chat):** "Make the Salesforce pipeline incremental — first run pulls everything, after that only changes since last run."
2. **Orchestrator:** Classifies → pipeline modification (resource policy change on EL artifact). Routes to EL specialist.
3. **Orchestrator → EL specialist:** Pre-scoped context includes the current `el/salesforce/__init__.py`, the curated source declaration in `src/carve/sources/salesforce/` (which already knows each object's natural cursor column — typically `LastModifiedDate` or `SystemModstamp`), conventions, and the matching dbt source's hints about primary keys.
4. **EL specialist:** Generates Plan with diffs to `el/salesforce/__init__.py`. For each requested resource:
   ```python
   @dlt.resource(
       write_disposition="merge",
       primary_key="Id",
       schema_contract={"tables": "evolve", "columns": "evolve", "data_type": "freeze"},
   )
   def accounts(
       updated_at = dlt.sources.incremental("LastModifiedDate", initial_value="2000-01-01")
   ):
       ...
   ```
   Schema contract follows **dlt's recommended default**: accept new tables and columns (most drift is harmless), freeze on data_type changes (those are usually bugs). Critical resources can override per-resource if the team wants stricter detection (UC5 covers that case).
   
   For `User` (small, easier to full-replace): `write_disposition="replace"`, no incremental cursor.
   
   Plus a one-line change to `pipelines/salesforce.toml`:
   ```toml
   [steps.extract_load.dlt]
   state_location = "destination"   # cursor state lives in Snowflake, not on disk
   ```
5. **Plan surfaces operational facts plainly:**
   - "First run after deploy will do a full load: estimated ~12M rows across 3 tables, ~25 min."
   - "Cursor state will be stored in `prod_db.raw_sfdc._dlt_pipeline_state` (managed by dlt)."
   - "Subsequent runs typically <1 min, only records modified since last cursor value."
   - "Recommended lookback overlap: 1 day, to catch late-arriving updates. Configurable in `pipelines/salesforce.toml`."
6. **Analyst (chat):** "Looks good. Build it."
7. **Orchestrator:** `carve build` writes the files locally.

**Verifying in dev (laptop)**

8. **Analyst:** `carve run salesforce --target dev`
9. **First dev run:** Full load (dev's destination is empty / had only old full-load data). Rows land; dlt writes cursor state to `dev_db.raw_sfdc._dlt_pipeline_state`. Took 6 minutes (smaller dev dataset).
10. **Analyst:** `carve run salesforce --target dev` again (second run).
11. **Second dev run:** dlt reads cursor from destination state table, only fetches records where `LastModifiedDate > <cursor>`. Few or zero rows. Took 8 seconds.
12. **Analyst (chat or CLI):** "Show me the current cursor state for salesforce." → `carve pipelines show salesforce --target dev` displays per-resource cursors and last load row counts. Sanity check passes.

**Deploying to prod**

13. **Analyst:** `carve deploy salesforce` → opens PR with the dlt source changes + `state_location` config change.
14. **PR review and CI:** Same as UC2. CI validates dlt syntax, checks that primary keys are declared on merge-disposition resources, parses without errors.
15. **PR merged. CI rolls out.** Prod `carve serve` restarts; reconciler is a no-op (schedule unchanged).

**First incremental-enabled prod run**

16. **Prod scheduler (next 15-min tick):** Fires the job as usual. Worker claims, runs `dlt pipeline run salesforce`.
17. **First run with the new code (in prod):** dlt sees no cursor state in `prod_db.raw_sfdc._dlt_pipeline_state` → **does a full historical load**. ~25 minutes.
    - The next 15-min tick fires while the first run is still going. Per-pipeline serialization (ARCHITECTURE §4.2) means the new tick lands in `queued` but does not start; the partial unique index on `(pipeline) WHERE status='queued'` then dedups subsequent ticks until the first run finishes.
    - One `schedule.skipped` event per ignored tick — visible in run history.
18. **First run completes.** Cursor state now exists in destination.
19. **Queued tick runs next:** ~8 seconds. Only incremental rows.
20. **All subsequent ticks:** incremental, fast.

**Analyst monitors (laptop, optional)**

21. `CARVE_SERVER_URL=https://carve.internal.co carve pipelines show salesforce --target prod` → current cursor values, last run rowcount, last 5 runs.
22. Slack webhook on the first long run posts a duration alert; subsequent fast runs land routine notifications.

### Re-loading historical data (later, ad-hoc)

Inevitable: analyst realizes they need to backfill data the cursor missed, or a column was added to the dlt source code that needs historical population.

Carve mirrors dlt's `--refresh` modes one-to-one — no Carve-invented aliases. The three modes:

- **`carve run salesforce --target prod --refresh drop_data`** — wipes state AND data for all resources, full reload on next run. The "true reload."
- **`carve run salesforce --target prod --refresh drop_data --resources accounts`** — same but scoped to one resource.
- **`carve run salesforce --target prod --refresh drop_resources`** — wipes resource state (cursor) but keeps loaded data. Use when you want to re-fetch from cursor reset without dropping data.
- **`carve run salesforce --target prod --refresh drop_sources`** — wipes all source-level state. Niche.

**Permission model (per option 2 decision):** Members can invoke any `--refresh` mode, including `drop_data` against prod. The confirmation prompt is the safety: `carve run --refresh drop_data` against prod prompts "this will drop state and reload all rows from source. Type 'salesforce' to confirm." Friction without bureaucracy. Admins skip the prompt only with `--yes` (CI flows). All `--refresh` invocations log to `run_events` with the actor token and reason.

### Design decisions surfaced

- **dlt state location is a Carve-managed choice surfaced in `pipelines/<name>.toml`** — `state_location = "destination"` (recommended for prod) or `"file"` (laptop default, fine for dev only).
  - Destination state: survives container restarts, works across multiple workers, naturally isolated per target by the destination schema.
  - File state: simpler but fragile in containerized prod.
  - `carve init` default to file for local; the EL specialist proposes `destination` whenever a pipeline has a prod schedule.
- **Per-resource incremental policy is encoded in the dlt source code itself**, not in pipeline TOML. The EL specialist generates the right decorators based on the curated source library's per-object metadata + analyst intent.
- **Plans surface incremental behavior in plain language** — first-run cost, ongoing cost, lookback window. Plain-language summary is a deliberate UX feature, not just JSON diff.
- **`carve pipelines show <name> --target <t>`** displays per-resource cursor state and last-run rowcount. New CLI surface; needs REST/MCP equivalents.
- **Per-pipeline serialization handles the "long first run + short cron interval" case** automatically — no additional logic needed. The `schedule.skipped` event is the existing signal.
- **Lookback overlap is a configurable per-pipeline knob** (`[dlt.incremental] lookback = "1d"` in pipeline TOML) defaulting to a small window to catch late-arriving updates. Without it, cursor-based incremental can miss records that arrive in the source after their `LastModifiedDate`.
- **`carve run --refresh <mode>` mirrors dlt's `--refresh` modes 1:1** (`drop_sources`, `drop_resources`, `drop_data`). No Carve-side renames. Confirmation prompt on `drop_data` against prod; member-invokable. All invocations logged to `run_events` with actor token.
- **Dev and prod cursors are isolated automatically** because they live in different destination schemas/databases (dlt's destination-side state takes care of this).
- **Default schema_contract follows dlt's recommendation:** `{tables: evolve, columns: evolve, data_type: freeze}`. Per-resource overrides allowed when a team wants stricter drift detection on critical objects (UC5).

### Resolved decisions

- **`state_location = "destination"` is the universal default for new pipelines** (laptop and prod). Same behavior everywhere, no "it worked on my laptop" surprises. Dev runs write a tiny state table to dev Snowflake costing cents — acceptable trade-off.
- **Lookback overlap default is per-source, declared in the curated source registry** (Salesforce: 1d; Stripe: 1h; HubSpot: 1d; defaults conservative). Analyst overrides per-pipeline as needed. No global default — sources differ too much.
- **`carve deploy` summary surfaces first-run duration estimates** ("first prod run estimated to load ~25 min; will run on next scheduled tick at HH:MM"). Transparency for the analyst shipping a plan they didn't write.

### Open questions

- **CDC vs cursor-based incremental.** Salesforce supports Change Data Capture streaming, but dlt's verified Salesforce source does **not** — it is cursor-based (`SystemModstamp` + merge disposition), and dlt ships no SaaS CDC source at all (only DB replication, e.g. Postgres `pg_replication`). v0.1: cursor-based only. Salesforce CDC would require custom native-source code consuming Salesforce's Streaming/CDC API and is a post-v0.1 enhancement. *(Verified against dlt 1.27.2 + dlt docs, 2026-06-12.)*
- **Pipeline-level vs. step-level incremental.** dlt's incremental is per-resource (inside the dlt source code). The pipeline TOML doesn't really know about it. This is fine but means `carve pipelines show` has to introspect dlt's destination-side state table to display per-resource cursors. Implementation detail worth pinning before specifying the CLI.

---

## Use Case 4 — Scheduled run fails after days of green; what does Carve do?

**Persona.** Same analyst from UC1–UC3 (or an on-call data engineer who didn't write the pipeline). Pipeline has been running cleanly every 15 minutes for 5 days. At 14:15 UTC, run #347 fails. A Slack notification arrives.

**Goal.** Get the pipeline back to green with the smallest reasonable human effort. Understand whether Carve auto-fixes things or just surfaces the problem.

**Where it happens.**
- Failure happens → prod `carve serve`
- Detection → prod runtime emits `run.failed`
- Diagnosis → prod runtime invokes the **recovery agent** (new specialist), produces a diagnosis + (when possible) a proposed fix plan
- Notification → Slack/email webhook with diagnosis link
- Human review → analyst's laptop, via chat/CLI pointed at prod
- Fix → same plan/build/deploy/PR loop from UC1; CI rollout to prod
- Resume → first successful run after the fix auto-clears the auto-pause

**Pre-conditions.**
- UC1–UC3 completed: pipeline in prod, scheduled `*/15`, incremental loading, several days of green runs
- `runtime.toml` has Slack webhook configured for `run.failed` and `incident.diagnosed` events
- Each step in `pipelines/salesforce.toml` declares `retries = 3` (per-step config in the pipeline TOML; default 3, overridable). `runtime.toml` has `[recovery] auto_diagnose = true`

### Direct answer

**Carve identifies and diagnoses; it does NOT auto-open a PR.** Proposed fixes are surfaced as reviewable Plans that the analyst accepts (or refines) before they go through the normal `build → deploy → PR → merge → CI rollout` flow. This preserves the PRD's no-autonomous-writes-to-prod rule from §6.3.

For failures Carve can't classify with confidence, it surfaces the diagnostic data (logs, schema diff, error class) without proposing a fix — the analyst debugs.

### Walkthrough (type change, which fails under dlt's recommended default)

We chose a type change as the failure scenario because it's what dlt's recommended `data_type: freeze` default actually catches — most "column added / removed" drift passes silently under the default. Schema-contract overrides for stricter detection are covered in UC5.

**Failure happens (prod)**

1. **14:15:00:** Scheduled tick fires; worker claims; subprocess `dlt pipeline run salesforce` starts.
2. **Salesforce admin had changed `Account.AnnualRevenue` from `Currency` (decimal) to `Text` 20 minutes earlier** (e.g., to allow free-form values like "Undisclosed"). dlt detects the type mismatch on extract — the existing destination column is BIGINT/DECIMAL, the new values are strings. Under the default `data_type: freeze`, dlt raises `DataValidationError` (`schema_entity=data_type`, `contract_mode=freeze`), surfaced at the top level as `PipelineStepFailed` with that exception as its `.exception`.
3. **Worker retries the step up to `retries = 3`** (per-step config from the pipeline TOML). Each retry hits the identical exception — type drift isn't transient.
4. **Retries exhausted.** Step marked failed, run #347 status → `failed`, full traceback persisted. Event emitted: `run.failed` with `pipeline=salesforce, target=prod, error_class=DataValidationError, retries_exhausted=true`.
5. **Schedule auto-paused immediately.** `schedules.salesforce.status = paused-by-recovery`, reason "retries exhausted: DataValidationError (data_type)". `schedule.paused` event fires; Slack: "⏸️ salesforce paused — investigating."

**Carve diagnoses automatically**

6. **Recovery agent triggers on the retries-exhausted `run.failed` event.** Pre-scoped context: failed run's log tail, pipeline TOML, current dlt source code, dlt's destination-side schema state (the cached "what we expect"), and the current source schema from `salesforce_describe_object`.
7. **Recovery agent classifies:** "data type change — `Account.AnnualRevenue` was `bigint` in cached schema, now arriving as `text` in source. `data_type: freeze` raised."
8. **Recovery agent produces a Plan** with file diffs to `el/salesforce/__init__.py`. Two options presented:
   - *Default proposal (safer):* keep `data_type: freeze`, but **add an explicit hint** for AnnualRevenue with `{"data_type": "text"}` so dlt accepts the new type. Note that historical destination data still numeric; downstream consumers (dbt models) may need updates.
   - *Alternative:* relax `data_type` to `evolve` for the accounts resource — dlt will widen automatically. Less strict; future type changes won't surface.
   - *Discard alternatives presented for completeness:* `data_type: discard_row` (drop rows with the new type), `discard_value` (drop the value but keep the row). **Both silently lose data** — surfaced as options for analysts who explicitly want this, with the warning called out in the proposal text.
9. **Recovery agent records an Investigation row** (new state-store entity) with: triggering `run_id`, `diagnosis_md`, `proposed_plan_id`, `status='proposed'`, `created_at`. Artifact at `.carve/investigations/<id>.json` on prod, mirrored to the state store.
10. **`incident.diagnosed` event emits.** Slack follow-up:
   > 🟡 *Investigation ready* — Carve diagnosed salesforce run #347 as data-type drift on `Account.AnnualRevenue` (bigint → text). Proposed fix: add a text-type hint. Review: `carve investigations show inv_abc123`.

**Analyst responds (laptop)**

11. **Analyst gets Slack ping. From chat (MCP pointed at prod):** "Show me the salesforce investigation."
12. **MCP tool returns the Investigation** — failed run logs (last 50 lines), diagnosis in markdown, proposed plan diff with the three options labeled.
13. **Analyst reviews diff:** the recovery agent's default is the text-hint approach. Analyst thinks about it — AnnualRevenue going to text means downstream dbt models that did numeric aggregations will break. Decides to take a more cautious path: keep numeric, push back to the Salesforce admin to revert the type change. Dismisses the investigation as "won't fix — source change being reverted."
14. **A week later** (after Salesforce admin reverts), retries succeed and pipeline resumes. Investigation status → `dismissed`. (Alternative path: if analyst had accepted the proposed plan, the rest is steps 14–22 from the previous version of this walkthrough.)

**Alternative path: analyst accepts the proposed fix**

15. **Analyst (chat):** "Looks good — take the text-hint option. Also flag in the dbt source that the column type changed."
16. **Orchestrator refines the plan**, includes a dbt source comment + a marker in the staging model for the type change.
17. **Analyst:** "Build and deploy."
18. **`carve build → deploy`** opens a PR. PR description auto-references investigation ID and failed run.
19. **PR reviewed. CI checks pass. Merged.**
20. **CI rollout completes; prod restarts with new code.**
21. **Deploy-event handler:** Investigation `inv_abc123` transitions to `resolved` (with `resolved_by_deploy_id`); paused schedule auto-resumes.
22. **`schedule.resumed` event fires.** Slack: "✅ salesforce auto-resumed — fix from inv_abc123 deployed." Next scheduled tick runs normally.

### What categories of failures get auto-diagnosed in v0.1?

Bounded list. The recovery agent's value depends on staying within what it can reliably classify.

Classification uses dlt's actual exception hierarchy — the recovery agent unwraps `PipelineStepFailed` to its `.exception` and matches on real classes (`DataValidationError`, `LoadClientJobFailed`, `DatabaseUndefinedRelation`, and the `DestinationTerminalException`/`DestinationTransientException` split) rather than a Carve-invented taxonomy.

| Category | Detect | Propose fix? |
|---|---|---|
| **Transient (network, rate limit, timeout)** | yes (`DestinationTransientException` + retry exhaustion) | No fix needed — retries usually catch it. If not, recommend tuning step's `retry` block |
| **Column added** (under stricter contracts) | yes (`DataValidationError`, `schema_entity=columns`) | yes — add explicit column, OR relax contract to `evolve`. Discard modes surfaced as options with data-loss warning |
| **Column removed** (under stricter contracts) | yes (`DataValidationError`, `schema_entity=columns`) | yes — remove from dlt resource. **Destination column NOT dropped** (dlt's behavior, Carve consistent) |
| **Data type change** | yes (`DataValidationError`, `schema_entity=data_type`) | yes — add explicit type hint, OR relax to `evolve` for auto-widening. Flag downstream dbt impact |
| **Primary key column missing from source** | yes (key absent in current source schema) | yes — propose updating `primary_key` declaration, OR alternative PK candidate. Critical: without PK, `merge` disposition silently produces duplicates |
| **Credential expired / OAuth revoked** | yes (no dlt auth class; matched on source-side HTTP 401/403 inside `.exception`) | no fix — instruct user to refresh creds; no automated path touches secrets |
| **Quota / rate limit hit** | yes (error class + retry exhaustion) | partial — propose increasing retry backoff or reducing batch size; human confirms |
| **Destination warehouse outage** | yes (`DatabaseUndefinedRelation` / `DestinationConnectionError` across multiple pipelines) | no code change — classify as infra outage; schedule will retry when destination recovers |
| **Source data invalid** (NOT NULL violation, PK conflict) | yes (`LoadClientJobFailed`, terminal load error) | no — data-quality issue, not code issue; surface logs, no plan |
| **dlt or dbt internal bug** | yes (error class outside expected set) | no — surface and recommend version pin / upgrade |
| **Truly novel / unclassified** | no | no — record logs in investigation, analyst owns it |

### Design decisions surfaced

- **New specialist: the recovery agent.** Triggered on retries-exhausted `run.failed`. Pre-scoped to one failed run. Allowed skills strictly read-only (no `write_file`, no `pipeline_*` mutators). Output: a diagnosis (markdown) plus, when possible, a proposed Plan via the normal Plan entity.
- **New state-store entity: `Investigation`** with columns `id, triggering_run_id, diagnosis_md, proposed_plan_id NULL, status, created_at, resolved_by_plan_id NULL, resolved_by_deploy_id NULL, recurring_run_ids JSONB`. Status set: `proposed | acknowledged | resolved | dismissed`.
- **Carve never auto-deploys a fix.** The proposed Plan is reviewable; it goes through build/deploy/PR like any other change. **Human in the loop is a hard v0.1 invariant.** The hosted product's plan-approval workflows extend it but don't replace it.
- **Retries-then-pause-then-diagnose.** Each step has `retries = N` (default 3, configurable per-step in pipeline TOML). When retries exhaust on a single run, the schedule auto-pauses immediately (status `paused-by-recovery`, distinct from `paused-by-code` from UC2 and `paused-by-user`). The recovery agent diagnoses in parallel and posts a follow-up notification when the proposed solution is ready.
- **Auto-resume on deploy of the resolving change.** Plans/Builds/Deploys carry an `investigation_id` through the chain. When the deploy lands and prod restarts with the new code, the matching investigation transitions to `resolved` and the paused schedule auto-resumes. If the fix doesn't work, the next scheduled run re-enters the retries-pause-diagnose cycle.
- **Recovery agent runs on dev-target failures too**, with the same flow. Pause logic applies if a dev schedule exists; ad-hoc dev runs just get diagnosed without anything to pause. Per-pipeline opt-out available via `[recovery] enabled = false`.
- **Investigation dedup.** Same error class + same pipeline + within a configurable window → recurring runs append `run_id` to the existing investigation's `recurring_run_ids` instead of creating a new investigation.
- **New CLI / REST / MCP surface:** `carve investigations list / show / dismiss`, `/api/v1/investigations`, MCP tools.
- **PR descriptions auto-link the investigation and failed run** when the plan came from a recovery flow. Closes the audit loop.
- **Recovery agent doesn't run for `failure_mode = warn` or `failure_mode = continue` steps** — those aren't real failures by the pipeline author's declaration.
- **Recovery agent's diagnosis can be refined via chat** before accepting. The proposed Plan is just a Plan; `plan --refine` works on it normally; investigation_id carries through refinements.

### Resolved decisions

- **Destination outage handling: skip auto-pause for infra-class failures.** Detection: error class indicates destination unreachable (connection refused, timeout from destination) AND ≥2 pipelines failing concurrently in the last 5 minutes. Classified as "infra outage, no action recommended"; no pause; no code-change proposal; retries pick up automatically when the destination recovers. Pause is reserved for code/schema/data issues that need human action.
- **Recurring-run list display cap.** `carve investigations show` displays the 10 most recent recurring run IDs with "... and N more (see `carve investigations show inv_xyz --all-runs`)". Same cap applied in MCP and REST responses.
- **LLM cost cap per day, configurable per-install.** `runtime.toml` setting `[recovery] daily_token_budget_usd = 5.00` (default $5/day). When exhausted, recovery agent stops invoking; failures still emit `run.failed` with logs but no automated diagnosis until the next day. Surfaces in Slack: "Recovery agent paused — daily budget exhausted, resumes at 00:00 UTC."
- **Investigation/Plan refinement relationship.** Refined plans are children of the recovery-proposed plan via `parent_plan_id`; `investigation_id` carries through. `resolved_by_plan_id` points at the final merged-and-deployed plan in the chain.

### Open questions

- **Multi-tenant in hosted.** Investigations need tenant scoping — covered by the ARCHITECTURE §9.9 multi-tenant pattern; flagged as a follow-up consistency check, not new design work.

### What's NOT in this use case (deferred)

- **Auto-deploying fixes without human review.** Considered and rejected. Crosses the no-autonomous-writes line; human-in-the-loop is a v0.1 invariant. May revisit post-v0.1 as opt-in for trivial categories (e.g., pure type-widening) with explicit per-pipeline allowlisting.
- **Cross-pipeline failure correlation** ("3 pipelines failed at the same time, common root cause = Snowflake outage"). Useful but adds substantial complexity. Defer.
- **A separate "incident commander" agent** for coordinated multi-pipeline outages. Overkill for v0.1.

---

## Use Case 5 — Source rename + remove + add in the same incident

**Persona.** On-call analyst (same as UC4, could be a different teammate).

**Goal.** Resolve a more dangerous schema drift than UC4: the Salesforce team made three different kinds of change at once. The pipeline fails on all three resources, and the recovery flow has to handle multiple distinct change types in a single Investigation.

**Where it happens.** Same as UC4 — failure surfaces in prod, recovery agent diagnoses, analyst reviews and refines from chat, fix lands via PR. The new wrinkle is that the proposed plan touches multiple resources with materially different change semantics.

**Pre-conditions.**
- UC1–UC4 completed
- Pipeline uses **dlt's recommended schema_contract by default** (per UC3), but the analyst has explicitly **overridden the `columns` scope to `freeze`** for `accounts`, `opportunities`, and `leads` because these objects have downstream dbt consumers that can't tolerate silent schema changes. The override lives per-resource in `el/salesforce/__init__.py`:
  ```python
  @dlt.resource(
      primary_key="Id",
      write_disposition="merge",
      schema_contract={"tables": "evolve", "columns": "freeze", "data_type": "freeze"},
  )
  def accounts(...): ...
  ```
- Salesforce team made three changes 30 minutes before the next scheduled run:
  - **Account**: `Industry` removed AND `IndustrySector` added (the Salesforce team's internal docs call it a rename; dlt sees two independent changes)
  - **Opportunity**: `LegacyForecastCategory` removed entirely
  - **Lead**: `LeadScore__c` (Number) added

Carve does not attempt to correlate the Account changes as a rename — dlt has no rename concept, and we follow dlt's worldview. The analyst, not Carve, decides whether to treat them as related when refining the plan.

### Walkthrough

**Failure**

1. **09:00:** Scheduled tick fires. Worker invokes `dlt pipeline run salesforce`.
2. **dlt processes resources:** Account, Opportunity, and Lead each fail schema-contract checks:
   - Account: `Industry` missing, `IndustrySector` present (unknown to the contract) → violation
   - Opportunity: `LegacyForecastCategory` missing → violation
   - Lead: `LeadScore__c` present (unknown to the contract) → violation
3. **Retries exhaust** (same outcome each retry — schema drift isn't transient). Run marked failed.
4. **Schedule auto-paused** (per UC4 retries-then-pause flow). Slack: "⏸️ salesforce paused — investigating."

**Diagnosis (multi-part)**

5. **Recovery agent triggers.** Pre-scoped context includes **dlt's destination-side cached schema** from the last successful run, the **current source schema** from `salesforce_describe_object` per object, and the failed run logs. Carve does not maintain its own source schema cache — it reads what dlt has already stored.
6. **Recovery agent computes a per-resource schema diff (against dlt's cached schema):**
   - **Account**: `Industry` missing in current source; `IndustrySector` present in current source but not in cached. Two independent changes — diagnosed as **one column removed + one column added**. No rename correlation attempted (dlt doesn't model renames; we don't either).
   - **Opportunity**: `LegacyForecastCategory` in cached but missing in current → **column removed**.
   - **Lead**: `LeadScore__c` present in current but not in cached → **column added**.
7. **Recovery agent generates a single Plan with multiple labeled sections** — one per change, not one per resource. The four labeled sections:
   - *Account — column removed (`Industry`):* Remove `Industry` from the dlt accounts resource. Destination column NOT dropped (dlt behavior; historical data preserved).
   - *Account — column added (`IndustrySector`):* Add `IndustrySector` to the dlt accounts resource. Diagnosis note: "If this is conceptually a rename of `Industry`, you'll likely want a dbt staging-layer alias to combine the historical `industry` values with new `industrysector` values. That belongs in dbt, not dlt — Carve does not propose the migration."
   - *Opportunity — column removed (`LegacyForecastCategory`):* Remove from the dlt opportunities resource. **Destination column preserved.**
   - *Lead — column added (`LeadScore__c`):* Add to the dlt leads resource. Note: "Historical Lead rows have no value for the new column. After deploy, run `carve run salesforce --target prod --refresh drop_data --resources leads` to backfill."
   
   For each "column added" section, the plan also surfaces the schema-contract alternatives the analyst can choose at refine time:
   - Default: explicit add to the resource (keeps `columns: freeze` strict, surfaces future drift).
   - Alternative: relax to `columns: evolve` for this resource (dlt absorbs future additions silently).
   - *Discard alternatives (surfaced with data-loss warning):* `columns: discard_row` (drop rows containing the unknown column), `discard_value` (drop the value, keep the row). Both silently lose data; presented so analysts know they exist.
8. **One Investigation row** with four diagnosis sections (1 add + 1 remove for Account, 1 remove for Opportunity, 1 add for Lead), one proposed Plan covering all changes. `incident.diagnosed` event emits; Slack: "🟡 *Investigation ready* — Carve diagnosed 4 changes (2 removed, 2 added) across 3 Salesforce objects. Review: `carve investigations show inv_xyz789`."

**Analyst reviews and refines**

9. **Analyst (chat):** "Show me the investigation."
10. **MCP returns the four-section diagnosis** with the proposed plan inline. Analyst sees:
    - Account: `Industry` removed + `IndustrySector` added — analyst checks Salesforce changelog, confirms the team's internal docs call it a rename. Decides to: take both proposed changes (remove + add) and follow up with a dbt staging-layer alias in a separate PR.
    - Opportunity: `LegacyForecastCategory` removed — confirms; notes for self to archive the destination column manually later.
    - Lead: `LeadScore__c` added — confirms; will run `--refresh drop_data --resources leads` after deploy.
11. **Analyst (chat):** "Take all four proposed changes. Add a code comment in the Account resource noting that this corresponds to a Salesforce-side rename of Industry → IndustrySector (per their changelog, 2026-05-21), and reference the follow-up dbt issue I'll open."
12. **Orchestrator refines the plan** — preserves the four resource changes as proposed, adds the comment annotation, references the not-yet-opened dbt issue placeholder.
13. **Analyst (separately, before building):** Opens a follow-up issue in the dbt repo: "Add staging-layer alias for accounts.industry → accounts.industrysector to unify historical and new values." Carve doesn't track the issue link — that's the analyst's responsibility — but the PR description will reference it.
14. **Analyst (chat):** "Build and deploy."
15. **`carve build → deploy`** opens one PR with all four changes. PR description auto-references inv_xyz789 and run #389 (per UC4).

**Merge, rollout, auto-resume**

16. **Reviewer sees one logical change with four labeled sections.** Approves. CI passes (the dlt source now matches the current Salesforce reality). Merged.
17. **CI rollout completes; prod restarts.**
18. **Deploy-event handler:** Investigation `inv_xyz789` transitions to `resolved`. Schedule auto-resumes (UC4 flow).
19. **Next scheduled tick:** Pipeline runs successfully — all resources match the source.

**Post-resolution backfills (analyst-initiated, separate from the resolution)**

20. **Analyst:** `carve run salesforce --target prod --refresh drop_data --resources leads`. Confirmation prompt: "this will drop state and reload all rows from source. Type 'salesforce' to confirm" (UC3 decision; member-invokable). Worker runs a full reload of the lead resource. Historical Leads now have `LeadScore__c` populated.
21. **For Account historical fill (`industry` → `industrysector`):** Out of scope for this incident. The follow-up dbt PR opened in step 13 adds a staging-layer alias mapping historical `industry` values to the new `industrysector` column. Belongs in dbt territory (v0.2 specifies the dbt-side ergonomics).
22. **For Opportunity column archival:** Analyst manually runs `ALTER TABLE prod_db.raw_sfdc.opportunities DROP COLUMN legacy_forecast_category` (or renames to `_legacy_*` for archival) outside of Carve. Carve neither suggests nor executes the DDL — same hard rule as dlt: destination column lifecycle is the analyst's.

### Design decisions surfaced

- **No rename concept; rename = delete + add.** Carve mirrors dlt's worldview exactly. The agent does not attempt naming/type heuristics to correlate "column gone" and "column appeared" into a rename. The analyst, not Carve, decides whether to treat them as related at refine time.
- **Per-resource schema_contract overrides are a first-class pattern.** Default follows dlt's recommendation, but critical resources can crank up to `columns: freeze` or `data_type: freeze` when downstream consumers can't tolerate silent change. The recovery agent reads the resource's actual contract to scope its diagnosis correctly.
- **One Investigation, one Plan, multiple labeled sections — one per change, not one per resource.** Multiple distinct schema events on the same resource (Account had both a removal and an addition) get separate sections in the same plan.
- **Carve reads dlt's existing destination-side schema state** as the "cached schema" input for diff. No Carve-side source-schema cache exists.
- **Carve never drops a destination column.** Hard rule, inherited from dlt's behavior — dlt itself never drops columns. We're consistent.
- **Carve never copies historical data between columns.** Rename backfill belongs in dbt (or one-off SQL). Carve flags the consideration in the diagnosis text but does not generate the migration.
- **Discard modes (`discard_row`, `discard_value`) are surfaced as options in the proposed plan**, with explicit data-loss warnings. They're not the recommended path, but analysts unaware of them shouldn't be deprived of the choice.
- **Backfill operations are recommendations in the diagnosis text, not Plan changes.** Plans only mutate code; `--refresh` is a runtime op the analyst runs explicitly with the standard confirmation prompt.
- **`investigation_id` carries through the Plan/Build/Deploy chain** even when the analyst refines (per UC4 design decision). Refinements don't break the resolution link.

### Resolved decisions

- **Recovery agent degrades gracefully when dlt's destination-side schema state is missing or stale** (first-ever failure, state table corrupted): "schema drift detected, but no cached schema to diff against; manual review required." Surfaces logs, no proposed plan.
- **Schema drift on separate runs.** If `Industry` is removed in one Salesforce change and `IndustrySector` is added a week later as a separate change, Carve sees two independent failures and produces two investigations (and two PRs). Known consequence of the no-rename-concept stance.

### Verification tasks — resolved (verified against dlt 1.27.2 + dlt docs, 2026-06-12)

- **Multi-resource failure execution in dlt → RESOLVED: dlt does *not* aggregate.** Extract runs round-robin (interleaved) by default, and a contract violation raises a single `PipelineStepFailed` that **aborts on the first failing resource** — one run never hands back a list of all failing resources. This is precisely why the recovery agent's section count is **diff-driven, not exception-driven**: step 6 above already builds sections from a per-resource schema diff (dlt's cached destination schema vs. the current source schema), which is the correct and only reliable approach. No walkthrough change needed; the context-building skill is specified to diff schemas, using the exception only to confirm the failure is schema-class.
- **dlt exception class names → RESOLVED.** The real public classes (replacing the `SchemaFrozenException` / `LoadJobFailed` stand-ins): schema-contract violation = `dlt.common.schema.exceptions.DataValidationError` (read `.schema_entity` ∈ {`columns`, `data_type`} and `.contract_mode` for the sub-split); terminal load failure = `dlt.load.exceptions.LoadClientJobFailed` (transient sibling `LoadClientJobRetry`); missing relation = `dlt.destinations.exceptions.DatabaseUndefinedRelation`; generic terminal/transient DB errors = `DatabaseTerminalException` / `DatabaseTransientException` / `DestinationConnectionError`. The top-level wrapper is always `dlt.pipeline.exceptions.PipelineStepFailed` — **unwrap `.exception`** and classify via `isinstance` against the `DestinationTerminalException` / `DestinationTransientException` base classes (survives leaf renames). **There is no native dlt auth-exception class** — auth failures are matched on source-side HTTP 401/403 inside `.exception`. The "small adapter layer" = this class→category table + the unwrap rule + base-class `isinstance` checks.

---

## Use Cases TBD

Future stories to walk through, roughly in priority order:

- **Production incident** — a scheduled run fails; analyst debugs from laptop, manually reruns against prod, patches via PR, redeploys
- **Onboard a new developer** — admin issues a token, new dev runs `carve login`, makes their first plan against a deployed pipeline
- **Modify a pipeline's steps or config** (not just schedule) — refinement loop, hot-reload of agent config
- **Schema drift in a source** — Salesforce adds a column; what does Carve do at the next scheduled run?
- **Audit / observability** — "what shipped to prod last week, and who triggered each manual run?"
- **Rollback a bad deploy** — merged PR introduced a regression; how does the team revert in prod?
- **Add a dbt model** that depends on the Salesforce data from UC1 (v0.2 territory but worth designing for)
- **Multi-step pipeline** — pipeline composes dlt + dbt + sql with cross-step outputs
- **Brownfield adoption** — existing dbt + dlt project, first `carve init`, convention inference
