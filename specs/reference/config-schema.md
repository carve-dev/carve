# Reference — Configuration schema

Carve configuration is split across a small set of TOML files under `carve/` at the project root. This document is the canonical reference for every file and field.

## File layout

```
<project-root>/
├── carve/
│   ├── carve.toml                  Project metadata, top-level settings
│   ├── connections.toml            Snowflake, Postgres, GitHub, etc.
│   ├── runner.toml                 Execution backend config
│   ├── guardrails.toml             Approval rules, cost limits
│   ├── mcp.toml                    External MCP servers
│   ├── conventions.md              Markdown — house style for the agents
│   ├── agents/                     Agent definitions (one TOML each)
│   │   ├── orchestration.toml
│   │   ├── dbt-engineer.toml
│   │   ├── snowflake-engineer.toml
│   │   └── quality.toml
│   ├── skills/                     Custom skills (Python or YAML)
│   ├── pipelines/                  Pipeline definitions
│   │   └── daily_revenue.toml
│   └── .carve/                     Generated state (gitignore)
│       ├── state.db
│       ├── plans/
│       └── runs/
```

## `carve.toml`

Project metadata. Small. Intentionally minimal — most config lives in dedicated files.

```toml
# carve/carve.toml

[project]
name = "jaffle-shop"
version = "0.1.0"
description = "Internal analytics for the bakery."

[carve]
required_version = ">=0.1,<0.2"

[paths]
dbt_project = ".."                 # Relative path to dbt project root
data_dir = "data"                  # Where ad-hoc data files live
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `project.name` | string | yes | — | Project identifier; appears in UI and logs |
| `project.version` | string | no | `"0.0.0"` | SemVer; bumped on releases |
| `project.description` | string | no | `""` | One-liner shown in UI header |
| `carve.required_version` | string | no | unbounded | PEP 440 specifier; doctor warns on mismatch |
| `paths.dbt_project` | string | yes if dbt used | `"."` | Path to `dbt_project.yml` |
| `paths.data_dir` | string | no | `"data"` | Local data directory for examples and tests |

## `carve/connections.toml`

Defines every named connection. Connections are referenced by name from steps and skills.

```toml
[snowflake.default]
account = { env = "SNOWFLAKE_ACCOUNT" }
user = { env = "SNOWFLAKE_USER" }
auth = "key_pair"
private_key = { env = "SNOWFLAKE_PRIVATE_KEY" }
role = "ANALYTICS"
warehouse = "TRANSFORM_WH"
database = "ANALYTICS"
schema = "DBT"

[snowflake.prod_readonly]
account = { env = "SNOWFLAKE_ACCOUNT" }
user = { env = "SNOWFLAKE_RO_USER" }
auth = "key_pair"
private_key = { env = "SNOWFLAKE_RO_PRIVATE_KEY" }
role = "READER"
warehouse = "READER_WH"

[github.default]
token = { env = "GITHUB_TOKEN" }
owner = "acme-data"
repo = "jaffle-shop"
default_branch = "main"

[postgres.replica]
host = { env = "POSTGRES_HOST" }
port = 5432
database = { env = "POSTGRES_DB" }
user = { env = "POSTGRES_USER" }
password = { env = "POSTGRES_PASSWORD" }
sslmode = "require"
```

### Common patterns

**Env-var indirection:** `{ env = "VAR_NAME" }` reads from environment. Carve never stores secrets in config files.

**File indirection:** `{ file = "/path/to/secret" }` reads from a file (Docker/K8s secrets pattern).

**Inline literal:** strings without indirection are literal. Discouraged for secrets.

### Snowflake auth modes

| Mode | Required fields |
|---|---|
| `password` | `password` |
| `key_pair` | `private_key` (PEM bytes or path), optional `private_key_passphrase` |
| `oauth` | `oauth_token` |
| `external_browser` | none — opens browser for SSO (interactive, dev only) |

### Connection types supported in M2/M3

`snowflake`, `github`, `postgres`, `mysql`, `bigquery` (M3+), `databricks` (post-1.0), `s3` (object store), `slack`, `pagerduty` (via MCP).

## `carve/runner.toml`

How steps execute. M1 ships only the local backend; future backends (Docker, Kubernetes, ECS) plug in here.

```toml
[runner]
backend = "local_venv"          # local_venv | docker | k8s

[runner.local_venv]
venv_dir = "carve/.venvs"
python = "python3.11"
default_timeout_seconds = 1800

[runner.docker]                  # M3+ when backend = "docker"
image = "ghcr.io/carve-org/runner:0.1.0"
network = "host"
mount_workdir = true

[runner.resources]               # Per-step defaults; overridable per step
memory_limit = "2g"
cpu_limit = "1.0"

[runner.logging]
level = "info"
retain_runs = 200                # How many runs to keep on disk
retain_days = 30
```

## `carve/guardrails.toml`

Rules the orchestration agent enforces before applying a plan.

```toml
[approval]
production = "always"            # always | never | risky_only
new_pipelines = "always"
schema_changes = "risky_only"

[cost]
warn_credits_per_run = 5.0
fail_credits_per_run = 50.0

[ddl]
allow_drop_table = false
allow_truncate = false
forbidden_schemas = ["RAW", "AUDIT"]   # No DDL ever from agents

[git]
require_pr_for_pipelines = true
require_review_count = 1
default_branch_protected = true
```

## `carve/mcp.toml`

External MCP servers Carve consumes. See [M3-04](../milestone-3-polish/04-mcp-client.md).

```toml
[servers.pagerduty]
type = "stdio"
command = "npx"
args = ["@pagerduty/mcp-server"]
env = { PAGERDUTY_TOKEN = { env = "PAGERDUTY_TOKEN" } }
enabled = true

[servers.notion]
type = "http"
url = "https://mcp.notion.com/v1"
auth = { bearer = { env = "NOTION_TOKEN" } }
enabled = true
```

## `carve/agents/<name>.toml`

Each agent is a single TOML file. The orchestration agent has access to other agents; specialists have access to skills.

```toml
# carve/agents/dbt-engineer.toml

name = "dbt-engineer"
description = "Authors and maintains dbt models."
model = "claude-sonnet-4"
max_tokens = 8192
temperature = 0.0

system_prompt = """
You are Carve's dbt specialist. You author SQL models that follow the project
conventions in conventions.md. You write tests for every new model. You do not
modify production data directly — only files that go through PR review.
"""

skills = [
    "dbt.read_manifest",
    "dbt.write_model",
    "dbt.run_tests",
    "dbt.compile",
    "schema.search",
    "snowflake.preview_query",
]

# Optional context loaded into every turn
context_files = [
    "carve/conventions.md",
    "../dbt_project.yml",
]
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | yes | — | Unique within project |
| `description` | string | yes | — | One-liner shown in UI |
| `model` | string | no | `"claude-sonnet-4"` | Anthropic model identifier |
| `max_tokens` | int | no | `8192` | Per-response cap |
| `temperature` | float | no | `0.0` | 0.0–1.0 |
| `system_prompt` | string | yes | — | Multi-line prompt |
| `skills` | list[string] | no | `[]` | Skill names to expose |
| `context_files` | list[string] | no | `[]` | Files prepended to context (relative to repo root) |
| `subagents` | list[string] | no | `[]` | For orchestration only — agents this one can delegate to |

## `carve/pipelines/<name>.toml`

Pipeline = DAG of steps. Each pipeline lives in its own file.

```toml
# carve/pipelines/daily_revenue.toml

[pipeline]
name = "daily_revenue"
description = "Ingest orders + customers, build mart_revenue."
schedule = "0 4 * * *"           # Cron, server timezone (UTC by default)
timezone = "America/Denver"      # Override timezone

[pipeline.notifications]
on_failure = ["slack:#data-alerts"]
on_success = []

[[steps]]
id = "ingest_orders"
type = "python"
depends_on = []
command = "python scripts/ingest_orders.py"
env = { S3_BUCKET = "acme-raw" }
timeout_seconds = 600
retries = 2

[[steps]]
id = "ingest_customers"
type = "python"
depends_on = []
command = "python scripts/ingest_customers.py"
timeout_seconds = 600

[[steps]]
id = "dbt_build"
type = "dbt"
depends_on = ["ingest_orders", "ingest_customers"]
command = "build"
selector = "tag:daily"
threads = 4

[[steps]]
id = "freshness_check"
type = "sql"
depends_on = ["dbt_build"]
connection = "snowflake.default"
sql_file = "queries/check_freshness.sql"
on_failure = "fail"

[[steps]]
id = "notify"
type = "shell"
depends_on = ["freshness_check"]
command = "bin/post_to_slack.sh"
on_failure = "warn"              # Don't fail the pipeline
```

### Step types

| Type | Required fields | See spec |
|---|---|---|
| `python` | `command` | M1-05 |
| `sql` | `connection`, (`sql` or `sql_file`) | M3-02 |
| `dbt` | `command` (run/build/test/seed/snapshot) | M2-05 |
| `shell` | `command` | M3-03 |
| `http` | `method`, `url` | M3-03 |
| `agent` | `agent`, `goal` | M2-02 |
| `approval` | `prompt` | M2-01 |

### Common step fields

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | string | — | Unique within pipeline |
| `type` | string | — | One of the types above |
| `depends_on` | list[string] | `[]` | Step IDs that must succeed first |
| `timeout_seconds` | int | `1800` | Hard timeout |
| `retries` | int | `0` | On transient failure |
| `retry_delay_seconds` | int | `30` | Initial delay; exponential backoff applied |
| `on_failure` | string | `"fail"` | `fail` \| `warn` \| `continue` |
| `env` | table | `{}` | Extra env vars; merged with runner defaults |
| `if` | string | `""` | CEL-like expression; step runs only if true |

## Validation

All configs are validated against pydantic schemas at load time. Errors are formatted with file path and line number where possible. Run `carve config validate` to check without applying.

```
$ carve config validate
✗ carve/pipelines/daily_revenue.toml:24
  Step "dbt_build" depends on "ingest_orders" but no such step exists.
  Did you mean "ingest_orders_v2"?
```

## Schema generation

The pydantic schemas backing this document are exported to `schemas/` in the Carve repo. Editor integration:

```jsonc
// .vscode/settings.json
{
  "json.schemas": [
    { "fileMatch": ["carve/pipelines/*.toml"], "url": "https://schemas.carve.dev/pipeline.json" },
    { "fileMatch": ["carve/agents/*.toml"],    "url": "https://schemas.carve.dev/agent.json" }
  ]
}
```

VSCode's TOML extension (Even Better TOML) honors these and gives autocomplete.
