# Changelog

All notable changes to Carve are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed (P1-03 — centralized config + per-target artifact layout)

- **`carve init` now produces a centralized config + per-target artifact
  layout.** Configuration lives at the project level
  (`carve/connections.toml`, `carve/runner.toml`, `carve/models.toml`,
  root `.env` / `.env.example`); only deployable artifacts live per
  target (`targets/<name>/el/`). The legacy top-level `pipelines/`
  directory is no longer created.
- `carve.toml` now seeds `[paths]` with both `agents_dir = "carve/agents"`
  and `targets_dir = "targets"`, so an advanced user can relocate the
  per-target tree if they want.
- `carve.toml` `[project] name` is detected from the project root's
  directory name at init time (replacing the hardcoded
  `"my-carve-project"`).
- `.env.example` ships `ANTHROPIC_API_KEY=` uncommented so a new user's
  first `cp .env.example .env` call yields a runnable file. The
  `# GITHUB_TOKEN=` line stays commented and gains a
  `# uncomment if using carve el deploy` clarifying suffix.
- `carve init` and `carve target create` now share a single
  `add_target_to_project` helper for the section + env-block + artifact
  dir; both verbs produce byte-identical artifacts.

#### Manual migration recipe (M1.1 → P1)

```bash
# 1. Move artifacts into the per-target tree
git mv pipelines targets/dev/el

# 2. Reshape carve/connections.toml from the old M1.1 single-target shape
#    to the new multi-section centralized shape:
#       [snowflake.dev]
#       account = "${DEV_SNOWFLAKE_ACCOUNT}"
#       ...

# 3. Rename env vars in .env to add the DEV_ prefix
#    (e.g. SNOWFLAKE_ACCOUNT → DEV_SNOWFLAKE_ACCOUNT).

# 4. Edit carve.toml: add `default_target = "dev"` under [project],
#    and `targets_dir = "targets"` under [paths].
```

There is no `carve migrate` command in v0.1; the recipe above is short
enough that the ceremony of an automated migration isn't worth the
maintenance.

### Added (M1.1-06 — pipeline-centric lifecycle)

- **`carve plan` is now design-only**: it produces a structured design
  document (no files written under `pipelines/`) and persists a `Plan`
  row with `phase = "drafted"`. The plan agent calls a `submit_plan`
  tool to finalize.
- **New `carve build <plan_id>` command** runs a separate **build agent**
  that consumes the design and writes `pipelines/<name>/main.py` and
  `requirements.txt`. On success the `Pipeline` row is upserted, the
  plan flips to `phase = "built"`, and the build run is recorded.
  `--force` rebuilds an already-built plan.
- **`carve run <pipeline_name>`** is the primary execution verb and is
  freely re-runnable. `carve run --plan <plan_id>` is supported for
  debug-replay against a specific built plan.
- **`carve plan --refine <plan_id> "<feedback>"`** produces a child plan
  with `parent_plan_id` set, plus a printed field-by-field diff of the
  prior design.
- **`carve plan --pipeline <name> "<change>"`** proposes a
  delta-consistent modification design; the existing `main.py` /
  `requirements.txt` are inlined into the agent's context.
- **New `pipelines` table** tracking each pipeline as a first-class
  entity (description, current plan, denormalised last-run status).
  `carve pipelines` lists all pipelines; `carve pipelines <name>` shows
  one pipeline's lineage and recent runs.
- Plans gained `phase` (`drafted` | `built`, CHECK-constrained) and an
  optional `pipeline_name` foreign key.
- Runs gained a `pipeline_name` foreign key. `carve runs --pipeline <name>`
  filters to a single pipeline's run history.
- **Alembic** is now wired into the state-store bootstrap; migrations
  live under `migrations/versions/`. A pre-Alembic dev DB is detected
  and stamped at the baseline, then upgraded to the pipeline-centric
  schema. The 0002 migration backfills a `Pipeline` row for every prior
  applied plan whose `task_graph_json` includes a `pipeline_dir`.

### Changed (M1.1-06)

- **The replay guard is gone from `carve run`.** Re-running a pipeline
  is the expected operation. (The guard moves to `carve apply` in M2,
  where idempotency matters for prod deploys.)
- The combined M1 code-agent prompt has been replaced by two narrower
  prompts: `m1_plan_agent.md` (design only — no `write_file` tool) and
  `m1_build_agent.md` (writes only `main.py` and `requirements.txt`,
  with the design pinned as a markdown preamble in the system prompt).
  M1.1-05's connection rules — no Python defaults for `SNOWFLAKE_*`
  env vars, explicit `role=` to `connect()`, no "How to Run" section —
  are folded into the build prompt.
- `carve apply <pipeline>` is now a reserved-verb stub printing an M2
  placeholder ("will create a prod-deploy PR; for dev execution use
  `carve run`"). Exits 0.

### Removed (M1.1-06)

- `Repository.mark_plan_applied` is replaced by
  `Repository.mark_plan_built`. `cli.orchestrator.applier.apply_plan`
  is replaced by `cli.orchestrator.runner.run_pipeline_by_name` /
  `run_pipeline_by_plan`.

### Added

- `carve plan` now prints live progress as the agent works: a spinner
  status line plus per-tool-call `→ name(args)` / `✓ summary` lines so
  the terminal no longer appears frozen for 30+ seconds. A `--quiet`
  (`-q`) flag suppresses the live output for CI/scripted use, leaving
  only the existing final plan summary. Internally this is driven by a
  new `AgentObserver` protocol on `AgentLoop`; M2 sinks (WebSocket,
  JSONL) can plug in without further loop changes.
- The CLI now auto-loads a project-local `.env` (defaulting to
  `<project-dir>/.env`, overridable with `--env-file`) before any command
  runs. Existing shell vars win — `.env` provides defaults only. Set
  `CARVE_NO_DOTENV=1` to disable for users managing env vars elsewhere
  (direnv, mise, 1Password CLI).

### Changed

- `carve init` now writes commented-but-complete templates for
  `connections.toml`, `models.toml`, `runner.toml`, and `.env.example`.
  A new user can fill in values without consulting Carve's source.
- `ModelsConfig.anthropic_api_key` is now optional at load-time; commands
  that need it (`plan`, `build`) raise a `ConfigError` pointing at
  `carve/models.toml` when the key is unset.
