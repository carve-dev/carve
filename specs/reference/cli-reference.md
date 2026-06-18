# Reference — CLI

The authoritative reference for the `carve` command surface. For programmatic / agent consumption, prefer the auto-generated OpenAPI schema (`GET /api/openapi.json`, spec 09) or the MCP tool listing (`tools/list`, spec 10) — the CLI, REST, and MCP surfaces are kept at parity.

> **Status:** matches the capability specs. A few commands are marked **(planned)** — referenced by the quick-reference or an upstream spec but without a defining body yet; they are called out inline. The completeness test (spec 13) asserts every Typer-registered command appears here once the CLI is built.

## Global flags

Available on every command:

- `--output [table|json|yaml]` — output format (default `table`)
- `--config-dir PATH` — override the project directory (default: search upward for `carve.toml`)
- `--server-url URL` — REST API base URL (default `http://127.0.0.1:8765`)
- `--verbose` / `--quiet` / `--no-color`
- `--help`, `--version`

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | User error (bad flag, missing argument) |
| 2 | Runtime error (e.g., a pipeline run failed) |
| 3 | Config error (invalid `carve.toml`, unresolvable component) |
| 4 | Drift detected (`carve deploy` pre-flight) |
| 5 | Server unreachable |

## Quick reference

| Command | Description | Spec |
|---|---|---|
| `carve init` | Bootstrap a Carve project | [init](../capabilities/init.md) |
| `carve plan "<goal>"` | Produce a reviewable plan (no files written) | M1.1, [dlt-engineer](../capabilities/dlt-engineer.md) |
| `carve build <plan_id>` | Materialize a plan into files | M1.1, [pipelines](../capabilities/pipelines.md) |
| `carve run <pipeline>` | Execute a pipeline on demand | M1.1, [runtime](../capabilities/runtime.md) |
| `carve runs` | List recent runs | M1.1 |
| `carve logs <run_id>` | Print / stream run logs | M1.1, [rest-api](../capabilities/rest-api.md) |
| `carve deploy <pipeline>` | Promote via a configurable handoff (default: PR) | [deploy](../capabilities/deploy.md) |
| `carve ask "<question>"` | Read-only investigative query (the explorer) | [ask](../capabilities/ask.md) |
| `carve serve` | Start the API + scheduler + reaper + archiver + workers | [runtime](../capabilities/runtime.md) |
| `carve worker` | Run a standalone worker process | [runtime](../capabilities/runtime.md) |
| `carve mcp-serve` | Start the MCP server (adapter over REST) | [mcp-server](../capabilities/mcp-server.md) |
| `carve schedule list/show/pause/resume/set-cron` | Live schedule controls (data, instant, audited) | [runtime](../capabilities/runtime.md) |
| `carve schedule reseed <pipeline>` | Re-apply `[seed_schedule]` from code to the live row | [pipelines](../capabilities/pipelines.md) |
| `carve pipelines list/show/validate/diff` | Pipeline definitions | [pipelines](../capabilities/pipelines.md) |
| `carve component <name> --separate-remote/-local/--same-repo` | Graduate / relocate a component | [pipelines](../capabilities/pipelines.md), [layout](../capabilities/layout.md) |
| `carve components show [<name>]` | List components (name, type, mode, resolved ref) | [pipelines](../capabilities/pipelines.md) |
| `carve memory show/edit/append-decision/refresh` | Project memory | [memory](../capabilities/memory.md) |
| `carve asks list/show` | Prior `ask` results | [ask](../capabilities/ask.md) |
| `carve investigations list/show/dismiss` | Recovery investigations | [recovery](../capabilities/recovery.md) |
| `carve agents list/show/create/edit/test` | Agent management (markdown definitions) | [extensibility](../capabilities/extensibility.md) |
| `carve skills list/show/test` | Skill registry (built-ins, packs, MCP) | [extensibility](../capabilities/extensibility.md) |
| `carve mcp-servers list/add/remove` | Register external MCP servers Carve consumes | [extensibility](../capabilities/extensibility.md) |
| `carve docs serve/regen/open` | Local static HTML UI | [ui](../capabilities/ui.md) |
| `carve workspaces list/clear` | Separate-remote workspace cache | [layout](../capabilities/layout.md) |
| `carve auth token rotate` | Mint/rotate the OSS API token | [rest-api](../capabilities/rest-api.md) |
| `carve metrics costs/runs/agents` | Aggregate metrics | [rest-api](../capabilities/rest-api.md) |

## Lifecycle verbs

### `carve init`

Bootstrap a Carve project. Configures four independent axes — Postgres state store, dbt presence, dlt presence, and project memory — interactively or via flags ([init](../capabilities/init.md)).

```
carve init [--external-postgres URL] [--with-dbt] [--dbt-path PATH | --dbt-url URL]
           [--with-dlt] [--dlt-path PATH | --dlt-url URL] [--default-target NAME]
           [--migrate-from-targets] [--non-interactive] [--project-name NAME]
```

```
carve init --with-dlt --dbt-path ./analytics --default-target dev
```

`--migrate-from-targets` migrates a legacy `targets/<t>/el/...` layout to the flat `el/<name>/` layout (one-shot, not undoable). Brownfield: point `--dbt-path` / `--dbt-url` at an existing dbt project; simple mode writes **no** `[components.*]` blocks (discovery is by convention — see [config-schema](./config-schema.md)).

### `carve plan` / `carve build` / `carve run`

```
carve plan "<goal>" [--pipeline <name>] [--refine <plan_id> "<feedback>"] [--target <name>]
carve build <plan_id> [--force]
carve run <pipeline> [--plan <plan_id>]
```

`plan` produces a reviewable design + a persisted Plan; it writes **no** files. `build` materializes a plan into files (dlt code, `pipelines/<name>.toml`) and emits the `post_build` hook ([extensibility](../capabilities/extensibility.md)). `run` executes an existing pipeline on demand.

```
carve plan "ingest the Stripe charges API into raw_stripe, then build the staging models"
carve build plan_a1b2c3
carve run daily_revenue
```

**(planned)** `carve run --watch` (stream until completion) and `carve run --resume <run_id>` (resume failed steps) are referenced by the quick-reference / MCP `run_resume` but not yet defined in a runtime spec body.

### `carve runs` / `carve logs`

```
carve runs [--pipeline <name>] [--status <s>] [--since <dur>] [--limit <n>]
carve logs <run_id> [--follow] [--step <id>]
```

```
carve runs --pipeline daily_revenue --status failed --since 7d
carve logs 4f6a... --follow
```

### `carve deploy`

Promote built code to a target via a **configurable handoff** ([deploy](../capabilities/deploy.md)). Default handoff is `pr`; cross-repo graduated components produce coordinated linked PRs (ingest-first). Emits the `pre_deploy` hook. The old `carve el deploy --from/--to` DDL-apply path is **retired**.

```
carve deploy <pipeline> [--target <name>] [--handoff files|commit|push|pr] [--amend] [--draft] [--yes]
carve deploy --reconcile-pins <pipeline>
```

```
carve deploy salesforce                 # default: open a PR
carve deploy salesforce --handoff push  # commit + push, no PR
```

Pre-flight drift detection exits `4` if the target's deployed state diverges from expectations.

### `carve ask`

Run the **explorer** — a read-only subagent that investigates the project (code, dbt manifest, dlt schema, `sql` introspection) and returns a cited answer. Changes nothing ([ask](../capabilities/ask.md)). Lineage questions are answered by investigation, not a stored graph ([lineage](../capabilities/lineage.md)).

```
carve ask "<question>" [--pipeline <name>] [--target <name>] [--output text|json] [--watch]
```

```
carve ask "where does net_revenue come from, and what breaks if I change raw_stripe.charges?"
```

## Runtime & serving

### `carve serve` / `carve worker`

```
carve serve [--port N] [--host H] [--workers N] [--no-scheduler] [--no-reaper] [--no-archiver]
carve worker [--workers N]
```

`carve serve` runs the FastAPI app (spec 09) plus the scheduler, reaper, archiver, and an in-process worker pool ([runtime](../capabilities/runtime.md)). `carve worker` runs standalone workers that claim jobs from the queue (optimistic `FOR UPDATE SKIP LOCKED`).

### `carve mcp-serve`

```
carve mcp-serve [--transport stdio|ws] [--port N] [--host H] [--server-url URL] [--token T]
```

Starts the MCP server, which adapts the REST surface to MCP tools ([mcp-server](../capabilities/mcp-server.md)). For *consuming* external MCP servers, see `carve mcp-servers`.

## `carve schedule ...`

The live schedule is **data** — these commands mutate the `schedules` table instantly (effective on the next scheduler tick, ≤ the loop interval), each audited in `schedule_changes`. No deploy, no PR ([runtime](../capabilities/runtime.md)).

```
carve schedule list
carve schedule show <pipeline>
carve schedule pause  <pipeline> [--reason "<text>"]
carve schedule resume <pipeline> [--reason "<text>"]
carve schedule set-cron <pipeline> "<cron>" [--timezone TZ] [--reason "<text>"]
carve schedule reseed <pipeline>      # re-apply [seed_schedule] from code (spec 08)
```

```
carve schedule set-cron daily_revenue "0 3 * * *" --timezone America/New_York
carve schedule pause daily_revenue --reason "investigating upstream outage"
```

`list` shows cron, timezone, paused state, and last/next fire (there is no separate `next-fires` command). `reseed` is the one path by which an edited `[seed_schedule]` block takes effect — otherwise code edits to the seed are inert.

## `carve pipelines ...`

```
carve pipelines list [--status <s>]
carve pipelines show <name>
carve pipelines validate [<name>]
carve pipelines diff <name> --against <build_id>
```

`validate` checks the TOML schema + the step DAG (unique ids, valid `depends_on`, no cycles, resolvable `component` names). Pipelines are authored via `carve plan` / `carve build`, not hand-scaffolded ([pipelines](../capabilities/pipelines.md)).

## `carve component` / `carve components`

Components are referenced by name; these commands manage where a component's code lives and how it's pinned ([pipelines](../capabilities/pipelines.md), [layout](../capabilities/layout.md)).

```
carve component <name> --separate-remote <url> [--ref <pin> | --branch <name>]
carve component <name> --separate-local <path>
carve component <name> --same-repo                # reverse a graduation
carve components show [<name>]
```

```
carve component analytics --separate-remote git@github.com:acme/analytics.git --ref 9f3a1c7
carve components show           # name, type, mode, resolved ref/path, referencing pipelines
```

## `carve memory ...`

Project memory: conventions (inferred), standards (authored), and the decision log ([memory](../capabilities/memory.md)).

```
carve memory show [<file>] [--pipeline <name>]
carve memory edit <file> [--direct]
carve memory append-decision "<title>" [--body "<text>"] [--reviewers a@,b@]
carve memory refresh [--backend dbt|dlt]
```

```
carve memory append-decision "Stripe retention is 18 months" --reviewers alice@
carve memory refresh --backend dbt
```

## `carve asks` / `carve investigations`

```
carve asks list [--since <dur>] [--pipeline <name>] [--limit N]
carve asks show <ask_id> [--include-trace]

carve investigations list [--status proposed|resolved|dismissed] [--since <dur>]
carve investigations show <id> [--all-runs]
carve investigations dismiss <id> --reason "<text>"
```

Investigations are produced by the recovery engineer on retries-exhausted failures ([recovery](../capabilities/recovery.md)); they carry a diagnosis + a proposed Plan that flows through the normal build/deploy path.

## Extensibility: `carve agents` / `carve skills` / `carve mcp-servers`

Agents are **markdown files with YAML frontmatter** (`carve/agents/<name>.md`); a user file overrides a built-in of the same name ([extensibility](../capabilities/extensibility.md)). See [config-schema](./config-schema.md) for the frontmatter fields.

```
carve agents list
carve agents show <name>
carve agents create <name> [--template <existing>]
carve agents edit <name>
carve agents test <name> "<prompt>"

carve skills list
carve skills show <name>
carve skills test <name> [--args '<json>']

carve mcp-servers list
carve mcp-servers add <name> --command "<cmd>" | --url <url>
carve mcp-servers remove <name>
```

```
carve agents create stripe-helper --template dlt-engineer
carve skills test dlt_schema --args '{"component":"stripe_charges"}'
carve mcp-servers add jira --command "jira-mcp"
```

External MCP tools are imported namespaced (`mcp:<server>:<tool>`) and effects-tagged; missing effect metadata fails closed (treated as writing). `carve mcp-serve` (Carve as a server) is separate from `carve mcp-servers` (external servers Carve consumes).

## Static HTML UI: `carve docs ...`

```
carve docs serve [--host H] [--port N] [--no-auto-regen] [--watch]
carve docs regen [--page <name>]
carve docs open
```

Serves the minimal local UI (run history, per-run detail + logs, pipelines) on loopback, regenerated on run events ([ui](../capabilities/ui.md)). No lineage view (deferred to a later increment).

## Auth & metrics

```
carve auth token rotate            # mint a new API token, write .carve/token (mode 0600), print plaintext
carve metrics costs|runs|agents [--since <dur>]
```

```
carve auth token rotate
carve metrics costs --since 30d
```

**(planned / thin):** `carve auth login` (OAuth to a Claude subscription) is referenced but not yet specified as a command — M1.1 configures auth via `auth_mode` in `models.toml`. `carve auth token mint`/`revoke` map to REST `POST`/`DELETE /api/v1/tokens`; only `rotate` has a defined CLI form today. The `carve metrics` subcommand spelling is defined by the quick-reference; the underlying data is the metrics router (spec 09).

## Deferred (a later increment)

- `carve target verify` — a small follow-up spec; the deploy pre-flight (spec 14) is the current readiness check.
- `carve el deploy`, `carve doctor`, `carve config`, `carve scaffold`, `carve dbt <passthrough>`, the old `carve mcp` group — **removed/retired**; superseded as noted above.
- Backfills, `carve run --step/--from/--backfill` — out of scope ([pipelines](../capabilities/pipelines.md)).

## Environment variables

- `DATABASE_URL` — Postgres connection string for the state store (consumed by `carve.toml`'s `[state_store]`)
- `ANTHROPIC_API_KEY` / OAuth (`auth_mode`) — model provider credentials (never passed into the `bash` tool's scrubbed env, spec 15)
- `CARVE_SERVER_URL` — default for `--server-url`
- Connector credentials (e.g. `STRIPE_API_KEY`) — referenced from `.dlt/secrets.toml` / `carve/connections.toml`, never stored in the state store

## Cross-references

- REST API: the OpenAPI schema at `/api/openapi.json` + `docs/api-reference.md` ([rest-api](../capabilities/rest-api.md))
- MCP tools: `docs/mcp-server.md` ([mcp-server](../capabilities/mcp-server.md))
- Config files: [config-schema.md](./config-schema.md)
- Vocabulary: [glossary.md](./glossary.md)
