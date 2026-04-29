# Reference — CLI

Every Carve command, in alphabetical order within each group. Output of `carve --help` is auto-generated from this same source via typer.

## Global flags

Available on every command.

| Flag | Description |
|---|---|
| `--config <path>` | Override config directory (default: `./carve/`) |
| `--profile <name>` | Override active profile (dev, staging, prod) |
| `--quiet`, `-q` | Suppress non-error output |
| `--verbose`, `-v` | Show debug logs (`-vv` for trace) |
| `--no-color` | Disable ANSI colors |
| `--json` | Machine-readable output where supported |
| `--version` | Print Carve version and exit |
| `--help`, `-h` | Command help |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic failure |
| 2 | Invalid usage (bad flags, missing args) |
| 3 | Configuration error |
| 4 | Authentication / connection error |
| 5 | Internal error / bug |

## Top-level commands

### `carve init`

Initialize Carve in the current directory.

```bash
carve init                        # Interactive — detect existing dbt, ask questions
carve init --greenfield           # New scaffold; no existing dbt
carve init --import .             # Brownfield — import existing dbt project here
carve init --import ../my-dbt     # Brownfield — dbt project elsewhere
carve init --example ecommerce    # Clone the e-commerce example
```

| Flag | Description |
|---|---|
| `--greenfield` | Scaffold new dbt + Carve project |
| `--import <path>` | Onboard onto an existing dbt project |
| `--example <name>` | Clone one of the official examples |
| `--non-interactive` | Use defaults, fail on missing values |

### `carve plan`

Ask Carve to plan how to achieve a goal. Produces a persisted plan that can be inspected and applied.

```bash
carve plan "ingest orders.csv into a staging table"
carve plan --pipeline daily_revenue "add a freshness check after dbt_build"
carve plan --agent dbt-engineer "add a customer_ltv mart"
```

| Flag | Description |
|---|---|
| `--pipeline <name>` | Scope the plan to an existing pipeline |
| `--agent <name>` | Force a specific specialist agent |
| `--output <path>` | Write plan to specific path (default: `.carve/plans/<id>.json`) |
| `--dry-run` | Don't persist the plan |

Output: a `Plan ID` that can be passed to `apply`.

### `carve apply`

Execute a previously-generated plan.

```bash
carve apply <plan-id>
carve apply <plan-id> --auto-approve
carve apply <plan-id> --pr-only        # Open the PR but don't trigger run
```

| Flag | Description |
|---|---|
| `--auto-approve` | Skip approval prompts (use carefully) |
| `--pr-only` | Create code changes / PR but don't execute pipeline |
| `--target <env>` | Target environment (dev/staging/prod) |

### `carve build`

Plan + apply in one shot. Convenience for the common case.

```bash
carve build "ingest orders.csv"
carve build --pipeline daily_revenue   # Re-plan and apply current pipeline
```

Same flags as `plan` plus `apply`.

### `carve run`

Execute an existing pipeline (or step) immediately, without re-planning.

```bash
carve run daily_revenue
carve run daily_revenue --step dbt_build
carve run daily_revenue --from dbt_build       # Re-run from this step onward
carve run daily_revenue --backfill 2025-04-01:2025-04-15
```

| Flag | Description |
|---|---|
| `--step <id>` | Run only one step |
| `--from <id>` | Re-run from this step onward (downstream subset) |
| `--backfill <range>` | Run for each date in `YYYY-MM-DD:YYYY-MM-DD` |
| `--params <key=val>` | Override parameters |

### `carve runs`

List recent runs.

```bash
carve runs
carve runs --pipeline daily_revenue
carve runs --status failed --since 7d
```

| Flag | Description |
|---|---|
| `--pipeline <name>` | Filter by pipeline |
| `--status <s>` | `running` \| `success` \| `failed` \| `cancelled` |
| `--since <duration>` | `1h`, `7d`, `2w` |
| `--limit <n>` | Max rows (default: 20) |

### `carve logs`

Stream or fetch logs for a run or step.

```bash
carve logs <run-id>
carve logs <run-id> --step dbt_build
carve logs <run-id> --follow
```

### `carve serve`

Start the Carve API server and web UI.

```bash
carve serve                      # localhost:8765
carve serve --port 9000
carve serve --host 0.0.0.0       # Bind on all interfaces (production)
carve serve --workers 4
```

### `carve doctor`

Run diagnostics. See [M3-13](../milestone-3-polish/13-doctor-command.md).

```bash
carve doctor
carve doctor --verbose
carve doctor --category connections
carve doctor --json
```

### `carve version`

Print version. Long-form includes Python version, install location, and dependency tree.

```bash
carve version
carve version --long
```

## `carve agent ...`

### `carve agent list`

List configured agents.

```bash
carve agent list
```

### `carve agent show <name>`

Show agent definition, current skills, recent runs.

```bash
carve agent show dbt-engineer
```

### `carve agent edit <name>`

Open the agent's TOML in `$EDITOR`. On save, validates and reloads.

```bash
carve agent edit dbt-engineer
```

### `carve agent test <name>`

Run a one-shot prompt against an agent without persisting state.

```bash
carve agent test dbt-engineer "what skills do you have access to?"
```

### `carve agent versions <name>`

Show change history (each `carve agent edit` is a versioned snapshot).

```bash
carve agent versions dbt-engineer
```

## `carve skill ...`

### `carve skill list`

List all skills available across agents.

```bash
carve skill list
carve skill list --agent dbt-engineer
```

### `carve skill show <name>`

Show skill definition (parameters, source, owning agent).

```bash
carve skill show schema.search
```

### `carve skill test <name>`

Invoke a skill directly with arguments.

```bash
carve skill test schema.search --args '{"query": "customer revenue"}'
```

## `carve pipeline ...`

### `carve pipeline list`

List pipelines and their status.

```bash
carve pipeline list
```

### `carve pipeline show <name>`

Show pipeline definition (rendered) and recent runs.

```bash
carve pipeline show daily_revenue
```

### `carve pipeline pause <name>` / `resume <name>`

Pause or resume scheduled execution.

```bash
carve pipeline pause daily_revenue
carve pipeline resume daily_revenue
```

### `carve pipeline history <name>`

Show run history with status and duration.

```bash
carve pipeline history daily_revenue --limit 50
```

## `carve dbt <args>`

Pass-through to dbt-core, with Carve's resolved profile and project paths.

```bash
carve dbt run --select stg_orders+
carve dbt test
carve dbt docs generate
```

Carve injects the profile and target derived from `carve.toml`, so users don't need separate `--profiles-dir` flags.

## `carve mcp ...`

### `carve mcp list`

List configured external MCP servers and their status.

```bash
carve mcp list
```

### `carve mcp add <name>`

Add an MCP server interactively.

```bash
carve mcp add pagerduty
```

### `carve mcp remove <name>`

Remove an MCP server.

```bash
carve mcp remove pagerduty
```

### `carve mcp tools <server>`

List tools exposed by a server.

```bash
carve mcp tools pagerduty
```

## `carve config ...`

### `carve config validate`

Validate all config files against schemas.

```bash
carve config validate
```

### `carve config show`

Print resolved config (merged + env-var-substituted, with secrets masked).

```bash
carve config show
carve config show pipelines/daily_revenue
```

### `carve config diff`

Diff against the version checked into git.

```bash
carve config diff
```

## `carve scaffold`

Generate boilerplate.

```bash
carve scaffold pipeline my_new_pipeline
carve scaffold agent custom-helper
carve scaffold skill my_skill --agent custom-helper
carve scaffold connection postgres replica
```

## Environment variables

Carve respects these globally:

| Variable | Default | Description |
|---|---|---|
| `CARVE_HOME` | `~/.carve` | Where global state lives |
| `CARVE_CONFIG_DIR` | `./carve` | Per-project config |
| `CARVE_LOG_LEVEL` | `info` | Override logging level |
| `CARVE_NO_TELEMETRY` | unset | If set, disables anonymous usage telemetry |
| `ANTHROPIC_API_KEY` | — | Required for agent loop |
| `ANTHROPIC_BASE_URL` | (default) | For self-hosted / Bedrock proxies |

## Shell completion

```bash
carve --install-completion bash
carve --install-completion zsh
carve --install-completion fish
```
