# dbt execution: running dbt as a pipeline step, across backends

> **dbt runs in many places, and Carve orchestrates it wherever it runs.** This capability executes a dbt component's models/tests/snapshots **as a pipeline step**, through a **pluggable backend** chosen to match how the team already runs dbt — Carve-bundled, the team's own dbt, dbt Cloud, or dbt-on-Snowflake-native. It is the control-plane thesis applied to transforms: *Carve composes/schedules/monitors the component; the component runs however it runs.* This is **separate from authoring** — see [`dbt-engineer`](./dbt-engineer.md). Execution is needed even with zero authoring (an orchestration-only dbt-Cloud or snowflake-native shop uses execution + scheduling + lineage and never has Carve write a model).

## Status

- **Status:** Drafting
- **Depends on:** [pipelines](./pipelines.md) (the `dbt` step type + step DAG), [runtime](./runtime.md) (the executor + **worker placement/labeling** — see *local placement*), [sql](./sql.md) (the dialect-aware connection Carve already holds — backs the snowflake-native trigger), [layout](./layout.md) (the dbt component config carries the backend), [connect](./connect.md) (lazy engine provisioning + pin on first use).
- **Used by:** [pipelines](./pipelines.md) (every `dbt` step dispatches here), [lineage](./lineage.md) (reads the manifest the backend exposes), [recovery](./recovery.md) (a failed dbt step's diagnosis).
- **Lineage:** net-new. Replaces the implicit "Carve always shells out to dbt-core" assumption (ARCHITECTURE §4.1/§8) and supersedes the retired "dbt Cloud as a step backend — out of v0.x" punt in PROJECT_PLAN.

## Goal

Run a dbt component's `build`/`run`/`test`/`snapshot`/`seed` **as a step in a composed pipeline**, through whichever backend matches the team's reality, behind **one backend-agnostic step interface**. The pipeline engineer wires a `dbt` step; *how* it executes is the component's configured backend; the step, status, logs, retries, and lineage look the same regardless.

## Out of scope

- **dbt authoring** — writing models/tests/sources is [`dbt-engineer`](./dbt-engineer.md). This capability *runs* dbt; it does not write it.
- **The warehouse/connection** — the destination (Snowflake, BigQuery, DuckDB, …) is a [connections/`sql`](./sql.md) concern. dbt connects to it; "where the SQL executes" (the warehouse) is a separate axis from "where the dbt process runs" (this capability).
- **Worker placement mechanics** — the *labeling/routing* that lets a dbt step run on a specific worker is owned by [runtime](./runtime.md); this capability *uses* it.

## Behavior

### The backend interface

Every backend implements one interface; the `dbt` step type calls it and never branches on backend:

```
DbtBackend.run(command, select, exclude, vars, target, full_refresh) -> DbtRunResult
  DbtRunResult: { status, per_model[], manifest_ref, run_results_ref, logs, duration, cost? }
```

`runtime` dispatches to the component's configured backend; results are normalized so `step_runs`, logs, events, and `lineage` are backend-uniform.

### Two families — by *who runs dbt*

Everything reduces to two families; this is the abstraction that keeps the backend list finite.

**A) `local` — Carve runs dbt** (subprocess on a Carve worker):

- **Engine** — `fusion` (the Apache-2.0 **dbt Core v2.0** Rust engine) or `dbt-core` (Python 1.x). The **bundled default is capability-detecting**: use Fusion where the warehouse adapter supports it (Snowflake, BigQuery, Databricks, Redshift) and **fall back to dbt-core** otherwise (DuckDB — Carve's first-class dev target — Postgres, and the long tail, which Fusion does not yet support). "Best engine the warehouse supports."
- **Environment** — `bundled` (Carve installs + pins the engine; see *provisioning*) or `external` (the team's own dbt: a venv/executable/Docker image + their `profiles.yml`/packages, for CI parity). Bundled = Carve owns the version; external = the user owns it.
- **Placement** — *which worker* runs it, via [runtime](./runtime.md) worker labels/routing. Default: any Carve worker. **"Run dbt on our own server" = a co-located Carve worker on that box** (option A) — it executes the step there, with that host's network/creds/VPC reach. No new dbt mechanism; it's worker placement.
- **Execution is always a subprocess**, never the in-process `dbtRunner` — dbt cannot safely run multiple invocations in one process, and Carve runs a concurrent worker pool. Reads `target/run_results.json` + `manifest.json`.

**B) managed — Carve *triggers + monitors*, never runs dbt:**

- **`snowflake-native`** — for [dbt Projects on Snowflake](https://docs.snowflake.com/en/user-guide/data-engineering/dbt-projects-on-snowflake) (GA Nov 2025). Carve **executes the dbt project object in Snowflake via SQL** (riding the Snowflake connection it already holds — see [`sql`](./sql.md)) and reads results from `QUERY_HISTORY` + the project's run output. Zero install anywhere; cheap for Carve. *(The primary backend for the Snowflake-centric buyer.)*
- **`dbt-cloud`** — trigger a configured dbt Cloud job via the **Administrative API v3** (`POST …/jobs/{id}/run/`), poll the run, fetch `run_results.json`/`manifest.json` from the artifacts API.
- **`remote`** — for "we run dbt on our own server but don't want Carve installed there" (option B): Carve **triggers the team's existing dbt run** (a webhook/endpoint, an SSH command, or kicking their CI job) and reads back what it exposes. Lightest footprint; thinnest/most-bespoke integration. *(Specifics in Open questions.)*

For all **managed** backends: **Carve's value is orchestrating the *full cross-tool pipeline* + AI** — its scheduler fires the pipeline (ingest → trigger the dbt run → …), not the platform's own scheduler (the Snowflake Task / dbt Cloud schedule is bypassed in favor of Carve owning the composed run).

### Engine provisioning + pinning (bundled only)

Carve does **not** front-load "install dbt + pick a version" at `carve init`. For the `bundled` env, the engine is **provisioned lazily on first dbt use by the [onboarding agent](./connect.md)**: it resolves the engine/version by warehouse (Fusion-or-core per above), installs it into the worker's managed environment, and **writes the pin back into the component config** (`dbt_engine`/`dbt_version`). Magical on first touch, **declarative and reproducible thereafter** — a lockfile, not a black box. A power user may **elect + pin eagerly** at init (`carve init --dbt-engine … --dbt-version …`) for offline installs or version policy.

**License guardrail:** the bundled engine is **dbt Core v2.0 (Apache 2.0)** — the OSS relicensing of the Fusion engine (June 2026) — **not** the ELv2 "dbt Fusion" commercial build, which forbids managed-service use and would taint OSS + hosted Carve.

### Component config (in `carve.toml`)

The backend lives on the dbt component ([layout](./layout.md)):

```toml
[components.analytics]
type = "dbt"
mode = "separate-remote"            # where the CODE lives (repo topology — orthogonal)
dbt_backend = "snowflake-native"    # local | snowflake-native | dbt-cloud | remote

# local-only:
#   dbt_engine = "fusion" | "dbt-core"   (pinned after first resolve; omit to auto-detect)
#   dbt_version = "2.0.x"
#   dbt_env = "bundled" | "external"     (external: dbt_path / dbt_image + profiles_dir)
#   worker_label = "onprem-dbt"          (placement, runtime)
# snowflake-native: snowflake_project = "<db.schema.object>"  (+ the sql connection)
# dbt-cloud:  account_id, job_id, api_token = "${DBT_CLOUD_TOKEN}"
# remote:     trigger = { kind = "webhook|ssh|ci", … }
```

### Status, output, and lineage per backend

Normalized to `DbtRunResult` regardless of source: local/external parse local `target/`; `snowflake-native` reads `QUERY_HISTORY` + the project run output; `dbt-cloud` fetches artifacts. **Lineage** ([lineage](./lineage.md)) reads `manifest.json` from wherever the backend exposes it. **When the engine is Fusion**, the manifest carries **SQL comprehension + column-level lineage** — so the column-level lineage deferred in [lineage](./lineage.md) arrives *for free from the engine* for Fusion/Snowflake users.

## Tests

- **Unit (dispatch):** the `dbt` step calls `DbtBackend.run` and normalizes results identically across a stubbed local / snowflake-native / dbt-cloud backend.
- **Unit (engine default):** Snowflake → Fusion (dbt Core v2.0); DuckDB/Postgres → dbt-core fallback; a resolved engine/version is pinned back into the component config and reused (not re-resolved).
- **Integration (local):** a `dbt build` runs as a subprocess (both engines), parses `run_results.json`, and surfaces per-model status; concurrent dbt steps run in separate processes.
- **Integration (snowflake-native):** Carve triggers the dbt project object via SQL and reconstructs status from `QUERY_HISTORY` + run output — no dbt installed.
- **Integration (dbt-cloud):** trigger via Admin API (mocked), poll to completion, fetch + parse artifacts.
- **Integration (placement):** a step tagged `worker_label = X` runs only on the worker advertising `X` (own-server case A).
- **Unit (remote):** a `remote` trigger fires the configured webhook/SSH/CI and ingests the returned result (case B).

## Acceptance

- A `dbt` step runs through each backend behind one step interface; status/logs/lineage are backend-uniform.
- The bundled default is **Fusion (dbt Core v2.0) where the adapter supports it, dbt-core otherwise**; the chosen engine/version is **pinned for reproducibility**; the bundled engine is Apache-2.0.
- **Managed backends trigger + monitor without Carve running dbt**; Carve's scheduler owns the composed pipeline (not the platform's).
- "Run dbt on our own server" works **both** ways: a **co-located worker** (A) and a **remote trigger** (B).
- For snowflake-native, Carve installs **no dbt** — it executes via SQL and reads `QUERY_HISTORY`.

## Design notes

- **Why pluggable backends?** It's the control-plane thesis: Carve references/orchestrates components however they run. dbt is where this bites hardest (dbt Cloud + dbt-on-Snowflake are huge among Carve's Snowflake-first buyers), and running bundled dbt-core *against* a project that's owned by dbt Cloud or Snowflake-native would be **wrong** (conflicting state/manifest/target/deferral), not just redundant.
- **Why two families, not N backends?** "Where does dbt run" always reduces to *Carve runs it on a worker* (placement handles "where", including your server) **or** *Carve triggers an external runtime*. New platforms (Databricks-native, …) are just more managed backends. The list stays finite.
- **Why subprocess, not in-process?** dbt cannot safely run concurrent invocations in one process; Carve's worker pool is concurrent. Subprocess is a correctness requirement, not a preference.
- **Why lazy-provision-then-pin?** Front-loading "install dbt, pick a version" at init asks a question the user often can't answer yet, and installs an engine they may never need (Cloud/native/external install nothing). Resolve-once-lock keeps it magical *and* reproducible.

## Open questions

- **`remote` trigger protocol.** Webhook vs SSH vs "kick your CI job" — which to ship, and the result-ingestion contract (how Carve gets structured status back from an arbitrary runner). *Smallest reasonable first cut: a signed webhook that returns a `run_results.json`-shaped payload.*
- **snowflake-native trigger mechanism.** Confirm the exact SQL surface to execute a dbt project object on demand (vs. relying on a Snowflake Task) and the cleanest way to read per-model results from `QUERY_HISTORY`/run output.
- **Worker-placement config shape.** Labels/selectors live in [runtime](./runtime.md); confirm how a component/step expresses a placement requirement (`worker_label`) and how it interacts with the hosted-Carve worker model.
- **Phasing.** Which backends ship in which increment is a [DELIVERY](../DELIVERY.md) decision, deliberately not set here. (Snowflake-native + bundled-local are the obvious early pair for the Snowflake buyer.)
