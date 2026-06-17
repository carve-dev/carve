# Reference — Configuration schema

The canonical reference for every file Carve reads or writes in v0.1. The executable source of truth is the Pydantic models in `src/carve/`; this document is the human-readable companion. For the control-plane model behind `carve.toml`, see [v0.1-03](../v0.1/03-flat-layout.md) and [`_strategy/control-plane-reference-model.md`](../_strategy/control-plane-reference-model.md).

## File layout

`carve.toml`, `pipelines/`, and `el/` live at the **project root** (the control plane references components by name). The `carve/` directory holds the config bundle + project memory.

```
<project-root>/
├── carve.toml                  # CONTROL PLANE: project meta, default target, [components.<name>]
├── carve/
│   ├── connections.toml        # target/connection definitions (+ dialect, role scoping)
│   ├── runtime.toml            # scheduler / worker / reaper / archive / recovery / permissions
│   ├── hooks.toml              # pre/post-tool + lifecycle hooks
│   ├── mcp.toml                # external MCP servers Carve consumes
│   ├── conventions.md          # inferred conventions (Carve-generated, refreshable)
│   ├── standards.md            # team standards (user-authored)
│   ├── decisions.md            # append-only decision log
│   ├── agents/
│   │   └── <name>.md           # agent definitions — MARKDOWN + YAML frontmatter (override built-ins)
│   └── skills/
│       └── <name>/SKILL.md     # skill packs (frontmatter + instructions + optional scripts/resources)
├── pipelines/
│   ├── <name>.toml             # pipeline composition: [pipeline], [seed_schedule], [[steps]]
│   └── <name>.md               # per-pipeline memory sidecar (optional)
├── el/
│   └── <name>/                 # a dlt component (simple mode: discovered by convention)
│       ├── __init__.py         # generated dlt source (provenance header) — refinable below the header
│       ├── requirements.txt    # pinned dlt deps
│       └── NOTES.md            # EL memory sidecar (optional)
├── .dlt/                       # dlt's own config (project-root scope)
│   ├── config.toml             # per-destination config (user-editable)
│   └── secrets.toml            # credentials (gitignored)
├── dbt_project.yml             # dbt project (same-repo mode; may be one level down)
├── docker-compose.yml          # bundled Postgres (Carve-templated)
├── .env / .env.example         # env vars (.env gitignored)
└── .carve/                     # generated runtime state (gitignored) — NO SQLite; state is in Postgres
    ├── token                   # OSS API token (mode 0600)
    ├── plans/<id>.json
    ├── asks/<id>.json
    ├── workspaces/<derived>/   # separate-remote component cache
    └── ui/                     # rendered static HTML
```

## Files Carve reads or writes

| File | Purpose | Owner | Spec |
|---|---|---|---|
| `carve.toml` | Control-plane config: project meta, `default_target`, `[state_store]`, `[components.<name>]` | Carve (init) + user | [03](../v0.1/03-flat-layout.md), [05](../v0.1/05-init-rewrite.md), [01](../v0.1/01-state-store-postgres.md) |
| `carve/connections.toml` | Target/connection definitions + dialect + credential references | User | [05](../v0.1/05-init-rewrite.md), [18](../v0.1/18-sql-layer.md) |
| `carve/runtime.toml` | Scheduler/worker/reaper/archive/recovery/permissions/CORS | User | [07](../v0.1/07-runtime.md), [15](../v0.1/15-agent-harness.md), [17](../v0.1/17-recovery-engineer.md) |
| `carve/hooks.toml` | Pre/post-tool + lifecycle hooks | User | [16](../v0.1/16-extensibility.md) |
| `carve/mcp.toml` | External MCP server registrations | User (`carve mcp-servers add`) | [16](../v0.1/16-extensibility.md) |
| `carve/conventions.md` | Inferred project conventions | Carve-generated (refreshable) | [05](../v0.1/05-init-rewrite.md), [06](../v0.1/06-project-memory.md) |
| `carve/standards.md` | Team standards (override conventions) | User-authored | [06](../v0.1/06-project-memory.md) |
| `carve/decisions.md` | Append-only dated decision log | User (append-only) | [06](../v0.1/06-project-memory.md) |
| `carve/agents/<name>.md` | Agent definitions — **markdown + YAML frontmatter** | User (override built-ins) | [16](../v0.1/16-extensibility.md) |
| `carve/skills/<name>/SKILL.md` | Skill packs (content, not callable tools) | User | [16](../v0.1/16-extensibility.md) |
| `pipelines/<name>.toml` | Pipeline composition | Carve-generated (refinable) | [08](../v0.1/08-multi-step-pipeline.md) |
| `pipelines/<name>.md` | Per-pipeline memory sidecar | User-authored | [06](../v0.1/06-project-memory.md) |
| `el/<name>/__init__.py` | Generated dlt source | Carve-generated (refinable below header) | [04](../v0.1/04-el-agent-dlt.md), [03](../v0.1/03-flat-layout.md) |
| `el/<name>/requirements.txt` | Pinned dlt deps | Carve-generated | [04](../v0.1/04-el-agent-dlt.md) |
| `.dlt/config.toml` / `.dlt/secrets.toml` | dlt's own config / credentials | User (secrets gitignored) | [03](../v0.1/03-flat-layout.md), dlt convention |
| `dbt_project.yml` | dbt project config (same-repo mode) | User (or `--with-dbt`) | dbt convention, [05](../v0.1/05-init-rewrite.md) |
| `docker-compose.yml` | Bundled Postgres | Carve-templated | [02](../v0.1/02-oss-packaging.md) |
| `.env` / `.env.example` | Env vars (`DATABASE_URL`, etc.) | User / Carve-templated | [05](../v0.1/05-init-rewrite.md) |
| `.carve/token` | OSS API token (mode 0600) | Carve-generated | [09](../v0.1/09-rest-api.md) |
| `.carve/plans/<id>.json` / `asks/<id>.json` | Plan / ask artifacts | Carve-generated | M1.1, [12](../v0.1/12-ask-verb.md) |
| `.carve/workspaces/<derived>/` | Separate-remote component cache | Carve-managed | [03](../v0.1/03-flat-layout.md) |
| `.carve/ui/` | Rendered static HTML | Carve-generated | [11](../v0.1/11-static-html-ui.md) |

> Investigations (recovery diagnoses) are **not** files — they are rows in the Postgres `investigations` table, surfaced via `carve investigations` ([17](../v0.1/17-recovery-engineer.md)).

## `carve.toml` — the control-plane config

The project root config. It references independently-versioned dlt/dbt components **by name** rather than containing them ([03](../v0.1/03-flat-layout.md)).

```toml
[project]
name = "jaffle-shop"
default_target = "dev"
carve_version = ">=0.1,<0.2"

[state_store]
url = "${DATABASE_URL}"          # env-var interpolation; Postgres only

# [components.<name>] blocks are OPTIONAL. Omit them entirely for "simple mode":
# components are then discovered by convention (each el/<name>/ is a dlt component;
# the single detected dbt project is a dbt component). Add blocks only when a
# component graduates to its own path/repo ("multi mode").

[components.analytics]
type = "dbt"                     # "dlt" | "dbt"
mode = "separate-remote"         # "same-repo" | "separate-local" | "separate-remote"
url  = "git@github.com:acme/analytics.git"   # separate-remote only
ref  = "9f3a1c7"                 # OPTIONAL pin (commit SHA or tag); see precedence below
# branch = "main"                # track a branch HEAD instead of pinning a ref

[components.stripe_charges]
type = "dlt"
mode = "separate-local"
path = "/path/to/ingest-stripe"  # required when mode == "separate-local"
```

**`[components.<name>]` fields:**

| Field | Type | Required when | Notes |
|---|---|---|---|
| `type` | `"dlt"\|"dbt"` | always | how the locator resolves + runs it |
| `mode` | `"same-repo"\|"separate-local"\|"separate-remote"` | always | explicit, not inferred |
| `path` | string | `mode="separate-local"` | code path; must exist |
| `url` | git URL | `mode="separate-remote"` | the component's repo |
| `ref` | commit SHA / tag | optional | a **pin** — checked out exactly |
| `branch` | string | optional | track this branch's HEAD if `ref` unset |
| `sync_mode` | `"hard"\|"soft"` | optional (default `hard`) | opt out of hard-reset sync |
| `sync_before_run` | bool | optional (default `true`) | set `false` for offline operation |

**Pin precedence:** `ref` wins (exact pin) → else `branch` (track that branch's HEAD) → else the remote's default-branch HEAD. Simple-mode (convention-discovered) components are never pinned — branch-HEAD, zero friction.

## `carve/connections.toml`

Named targets (connections). Each carries a **dialect** (Snowflake + DuckDB first-class in v0.1; Postgres/BigQuery/Databricks/SQL Server via `sqlglot`, introspection hardened post-v0.1, [18](../v0.1/18-sql-layer.md)) and is role-scoped (read vs write/deploy). Credentials are referenced, never inlined.

```toml
[targets.dev]
dialect = "duckdb"
path = "./dev.duckdb"

[targets.prod]
dialect = "snowflake"
account = "${SNOWFLAKE_ACCOUNT}"
user = "${SNOWFLAKE_USER}"
auth = "key_pair"                # password | key_pair | oauth | external_browser
private_key = "${SNOWFLAKE_PRIVATE_KEY}"
role = "TRANSFORM"               # write/deploy role
read_role = "READER"             # optional: a lower-privilege role for read-only introspection
warehouse = "TRANSFORM_WH"
database = "ANALYTICS"
schema = "DBT"
```

Credential indirection: `${ENV_VAR}` (environment) or `{ file = "/path" }` (Docker/K8s secrets). Carve never stores secrets in the state store ([15](../v0.1/15-agent-harness.md)).

## `carve/runtime.toml`

Tunes the runtime. (This replaces the pre-v0.1 `runner.toml`; there is no `backend = docker/k8s` axis — v0.1 is a single Postgres-backed queue + worker pool.)

```toml
[scheduler]
interval_s = 30                  # cron-evaluation loop interval

[reaper]
interval_s = 30
stale_threshold_s = 60           # missed heartbeats → reclaim a crashed worker's jobs

[worker]
intra_pipeline_slots = 4         # parallel steps per worker

[runtime.archive]
interval_s = 3600                # hourly active→archive sweep
jobs_window = "7d"
runs_window = "30d"
logs_window = "30d"

[recovery]
enabled = true                   # per-pipeline opt-out: set false to disable diagnosis
daily_token_budget_usd = 5       # spent → log-only, no diagnosis until reset

[permissions]                    # TIGHTEN-ONLY: narrows mode defaults, never widens them (spec 15)
# effective tool/bash grant = mode-default ∩ this config ∩ the agent's frontmatter

[api.cors]
allowed_origins = ["http://127.0.0.1:*"]   # OSS default: loopback for the static UI
```

## `carve/agents/<name>.md` — markdown + YAML frontmatter

Agents are **markdown files**, not TOML. Built-ins live at `src/carve/core/agents/builtin/<name>.md`; a user file at `carve/agents/<name>.md` **overrides a built-in of the same name** ([16](../v0.1/16-extensibility.md)).

```markdown
---
name: dlt-engineer
description: Authors and runs dlt sources/pipelines. Use for ingest / extract-load goals.
model: claude-{LATEST_SONNET}            # optional; per-agent tier; falls back to install default
tools: [edit, create_file, bash, grep, glob, web_fetch, dlt_library, sql]
allowed_paths: ["el/**", ".dlt/*.template"]   # write scope, ENFORCED by the permission gate
max_mode: build                          # ADVISORY lint + clamp; the runtime gate is the boundary
classifications: [new_pipeline, modify_pipeline, refactor_to_incremental]
---
You are Carve's dlt engineer. You author dlt sources/resources that follow the
project conventions, run them via `dlt pipeline run`, and self-correct on the
parsed result before returning a proposed Plan…
```

| Frontmatter field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | unique within a discovery root; user file overrides built-in |
| `description` | string | yes | used for orchestrator routing + `carve agents show` |
| `model` | string | no | per-agent model tier; falls back to install default |
| `tools` | list[string] | no | base tools (spec 15) + skills (spec 16); `mcp:<server>:<tool>` allowed |
| `allowed_paths` | list[glob] | no | write confinement, enforced by the gate |
| `max_mode` | `read_only\|plan\|build\|deploy` | no | **advisory** — the runtime gate is authoritative |
| `classifications` | list[string] | no | goal classifications this agent handles |

**Key semantics:** grants are runtime *attenuation* (effective set = `grant ∩ mode-permitted`), so a user override can't raise the effective mode or escape `allowed_paths`; loading is inert (safe YAML, no code execution); hot-reload happens at dispatch time, never mid-conversation.

## `carve/skills/<name>/SKILL.md`

A skill **pack** is a folder: `SKILL.md` (frontmatter + instructions) plus optional `scripts/` / `resources/` (e.g., a curated dlt source). A pack surfaces as **description-matched content injected into context** — not a callable tool ([16](../v0.1/16-extensibility.md)). The curated connector library ships as skill packs.

```markdown
---
name: stripe
description: Curated dlt source for the Stripe API. Use when ingesting Stripe data.
expects_env: [STRIPE_API_KEY]
---
How to use the bundled dlt source, validation glue, and conventions…
```

Built-in *callable* skills (`@skill` functions — catalog introspection, `dbt_manifest`, `dlt_schema`, `memory_read`) are registered in `src/carve/core/skills/builtin/__init__.py`, separate from packs.

## `carve/hooks.toml`

User-defined hooks at tool and lifecycle seams ([16](../v0.1/16-extensibility.md)).

```toml
[[hook]]
on = "pre_tool"                                       # pre_tool | post_tool | pre_deploy | post_build | on_run_failed
match = { tool = "bash", command = "git commit*" }    # optional matcher
run = "sqlfluff lint --dialect snowflake {changed_sql}"   # non-zero exit BLOCKS the action

[[hook]]
on = "on_run_failed"
run = "notify-slack {pipeline} {error}"
```

Hooks run through the **same `bash` gate** (no bypass), are mode-clamped, never recurse into `pre_tool`, and **fail closed** (an error/timeout blocks the action). Emission points: `pre_tool`/`post_tool` (spec 15, after the gate admits the call), `pre_deploy` (spec 14), `post_build` (spec 08), `on_run_failed` (subscribes to spec 07's `run.failed`).

## `carve/mcp.toml`

External MCP servers Carve consumes (managed via `carve mcp-servers`). Tools import namespaced (`mcp:<server>:<tool>`) and **effects-tagged**; missing effect metadata fails closed (treated as writing) ([16](../v0.1/16-extensibility.md)).

```toml
[servers.jira]
transport = "stdio"              # stdio | http
command = "jira-mcp"
enabled = true

[servers.notion]
transport = "http"
url = "https://mcp.notion.com/v1"
auth = { bearer = "${NOTION_TOKEN}" }
enabled = true
```

## `pipelines/<name>.toml`

A pipeline composes components **by name** into a step DAG ([08](../v0.1/08-multi-step-pipeline.md)). The schedule lives in a `[seed_schedule]` block — a one-time **seed**, not the live source of truth (the live schedule is data in the `schedules` table).

```toml
[pipeline]
description = "Stripe charges ingest + staging + search refresh"
owner = "data-team"

# Applied ONLY at first registration. Editing it later is a no-op unless you run
# `carve schedule reseed <pipeline>`. There is NO `paused`/`enabled` key here —
# pause/resume is live data (carve schedule pause/resume).
[seed_schedule]
cron = "0 2 * * *"
timezone = "UTC"
target = "prod"

[[steps]]
id = "ingest_stripe"
type = "dlt"                     # dlt | dbt | sql  (only these three in v0.1)
component = "stripe_charges"     # NAME → el/stripe_charges/ (simple) or remote repo @ ref (multi)
depends_on = []
[steps.failure_mode]
mode = "retry"                   # fail | warn | continue | retry | skip_downstream
max_attempts = 3
backoff = "exponential"          # exponential | linear | fixed

[[steps]]
id = "stage_stripe"
type = "dbt"
component = "analytics"          # OPTIONAL in simple mode (single detected dbt project)
command = "build"                # build | run | test | snapshot | seed
select = "stg_stripe_charges+"
depends_on = ["ingest_stripe"]

[[steps]]
id = "refresh_search"
type = "sql"
file = "sql/refresh_charges_search.sql"   # sql steps reference a FILE + connection, not a component
connection = "prod"                        # target name from carve/connections.toml
depends_on = ["stage_stripe"]
[steps.failure_mode]
mode = "warn"
```

**Step config:** `dlt` steps require `component` (+ optional `write_disposition`, `resource_select`); `dbt` steps take an optional `component` + `command`/`select`/`exclude`/`vars`/`full_refresh`; `sql` steps take `file` + `connection`. Cross-step values flow via `[steps.<id>.jinja_vars]` referencing `{{ steps.<other>.outputs.* }}`. The old `artifact = ...` key is rejected with a migration message (renamed to `component`).

## Project memory

`carve/conventions.md` (Carve-generated by inference, refreshable via `carve memory refresh`), `carve/standards.md` (user-authored, overrides conventions), and `carve/decisions.md` (append-only dated log via `carve memory append-decision`). Optional per-scope sidecars: `pipelines/<name>.md`, `el/<name>/NOTES.md`. All are read into agent context ([06](../v0.1/06-project-memory.md)).

## `el/<name>/` — a dlt component

`__init__.py` carries a **provenance header** recording what generated it and from where; Carve regenerates below the header and preserves user edits, and never touches a file lacking the header (treated as user-authored). `requirements.txt` pins dlt deps. The DLT engineer writes `.dlt/config.toml.template` / `.dlt/secrets.toml.template` — never the live `.dlt/secrets.toml` ([03](../v0.1/03-flat-layout.md), [04](../v0.1/04-el-agent-dlt.md)).

## `.carve/` — generated runtime state (gitignored)

Postgres holds run history, plans, builds, schedules, investigations, etc. — **there is no SQLite `state.db`** (spec 01). `.carve/` holds only local artifacts: `token` (mode 0600), `plans/<id>.json`, `asks/<id>.json` (answer + tool-call trace), `workspaces/<derived-name>/` (separate-remote clones, derived from `slugify(url)-ref`), `ui/` (rendered HTML).

## Validation

Configs are validated against Pydantic models at load time, with file + line context. `carve pipelines validate [<name>]` checks pipeline TOML + the step DAG (unique ids, valid `depends_on`, no cycles, resolvable `component` names) ([08](../v0.1/08-multi-step-pipeline.md)). The Pydantic models in `src/carve/` are the executable source of truth; this doc is the human companion.

## Cross-references

- The control-plane model: [v0.1-03](../v0.1/03-flat-layout.md), [`_strategy/control-plane-reference-model.md`](../_strategy/control-plane-reference-model.md)
- CLI that reads/writes these: [cli-reference.md](./cli-reference.md)
- Vocabulary: [glossary.md](./glossary.md)
