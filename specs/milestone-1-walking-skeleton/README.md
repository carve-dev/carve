# Milestone 1 — Walking skeleton

**Duration:** 1 week (days 1-7)
**Goal:** the smallest end-to-end loop that proves an agent can take a natural-language request, generate working Python that connects to Snowflake, and execute it.

## Acceptance criteria

A new user can:

1. Clone the Carve repo and install it locally
2. Run `carve init` in a fresh directory
3. Edit the generated `carve/connections.toml` with their Snowflake credentials
4. Run `carve plan "ingest a CSV from a public URL into a Snowflake table"`
5. See a sensible plan printed to the terminal
6. Run `carve apply <plan_id>`
7. Watch a Python script execute, connect to their Snowflake, and write data
8. See the run recorded in the state store via `carve runs`

The whole flow takes under 10 minutes, all from the CLI. **No web UI, no PR creation, no dbt integration.**

## What ships

- `carve init`, `carve plan`, `carve apply`, `carve run`, `carve runs`, `carve logs` commands
- `carve.toml` parser with multi-file support
- SQLite state store with `runs`, `logs`, `plans` tables
- Anthropic API client with tool-use loop
- One combined "code" agent
- `python` step type and `LocalVenvRunner`
- Snowflake connector

## What is explicitly deferred

- Web UI of any kind
- Plan/apply with PR creation (apply just runs immediately)
- dbt integration
- Multiple agents (one combined agent for now)
- Skills as a discoverable concept (the agent has hardcoded tools)
- MCP integration
- Multi-step pipelines (single Python step only)
- Schema embedding
- Brownfield onboarding
- Convention inference

## Spec list

In recommended build order:

1. [`01-cli-foundation.md`](./01-cli-foundation.md) — project skeleton, CLI framework, command stubs (day 1)
2. [`02-config-loader.md`](./02-config-loader.md) — `carve.toml` parsing, validation, env interpolation (day 2)
3. [`03-state-store.md`](./03-state-store.md) — SQLite + SQLAlchemy + repository pattern (day 2)
4. [`04-anthropic-agent-loop.md`](./04-anthropic-agent-loop.md) — Anthropic SDK wrapper with tool-use turn-taking (day 3)
5. [`05-python-step-and-runner.md`](./05-python-step-and-runner.md) — Step protocol, Python step, LocalVenvRunner (day 4)
6. [`06-snowflake-connector.md`](./06-snowflake-connector.md) — Snowflake connection wrapper (day 4)

Days 5-7 are integration and buffer.

## Risk

The single biggest M1 risk is the agent loop being harder than expected. If the agent struggles with broad goals, narrow the M1 demo target until something works. Don't get stuck trying to make the agent handle every CSV format — pick one specific public CSV and hard-code the demo around it if needed.

## Definition of done

- All six specs are implemented and have tests
- The acceptance criteria above are met when run by someone other than the author
- The README has a complete walkthrough of the demo flow
- Internal tag `v0.0.1`
