# M1.1-05 — Tighten the M1 code agent prompt

**Milestone:** 1.1 — M1 follow-ups
**Estimated effort:** 0.25 day
**Dependencies:** M1-04 (agent loop), M1 integration (planner)

## Purpose

The first real M1 plan ran end-to-end but produced two avoidable rough edges:

1. The agent picked `RAW_US_CENSUS` as the destination database — a value it pulled from a `SHOW DATABASES` result during planning, not from the user's connection config. The runtime env injection (`SNOWFLAKE_DATABASE` from `connections.toml`) saves the day on `apply`, but the user-facing plan summary mentions a database that has nothing to do with the user's setup, which is confusing and erodes trust.

2. The agent's final summary included a `## How to Run` section telling the user to `pip install -r requirements.txt`, `export SNOWFLAKE_*`, and `python pipelines/.../main.py`. Those steps bypass the Carve runner entirely, contradict the actual `carve apply` workflow, and make Carve look optional to its own pipelines.

Both are prompt issues. Fix the prompt, give the agent more context up front, and ban the "How to Run" pattern.

The dedicated pipeline-specialist agents (M2) will own deeper data-engineering concerns (incremental loading, schema drift, checkpointing). This spec only fixes the M1 ergonomic gaps.

## Scope

### In scope

- A `Connection context:` preamble injected into the system prompt at plan time, listing the active target's database, schema, role, and warehouse — pulled from `Config.connections.snowflake[default_target]`. The agent stops having to guess.
- Prompt rules for **destination** decisions:
  - Use `${SNOWFLAKE_DATABASE}` from env, no fallback default in the script.
  - Use `${SNOWFLAKE_SCHEMA}` by default; only create a new schema if the user's goal explicitly asked for one or if the source data clearly warrants its own namespace.
  - When in doubt, ask in the summary rather than hardcoding.
- Prompt rules for the **final summary** the agent emits:
  - Describe what was built. Mention the destination database/schema/table.
  - Tell the user `carve apply <plan_id>` will run it. **Do not** include `pip install`, `export`, or `python` instructions; the runner handles those.
  - Keep it short — bullet-list under ~150 words.
- A regression test that mocks the agent's response to include a "How to Run" section and asserts a postprocessor strips or rejects it. (Belt and suspenders — the prompt is the primary defense, the postprocessor is the safety net.)

### Out of scope

- Specialist agents per pipeline class (ingest, transform, dbt). M2 introduces those.
- Incremental ingest, schema drift, checkpointing, idempotent pagination — all deferred to the specialist agents.
- Changes to the tool set (`read_file` / `write_file` / `run_snowflake_query`).
- Multi-target awareness (using a non-default target during plan). Default-target only is enough for M1.
- Rewriting plans the user has already generated. The fix applies to new `carve plan` invocations; `carve apply` on a pre-fix plan still works because the runtime env injection masks the bad defaults.

## Implementation

### File: `src/carve/core/agents/prompts/m1_code_agent.md`

Restructure the existing prompt to remove the implicit invitation to write "How to Run" instructions and add destination-selection rules. Concretely, replace the current body with something like:

```markdown
You are Carve's code agent. Your job is to help users build data pipelines that
ingest source data into Snowflake.

When given a goal, you will:
1. Use `read_file` to understand the user's existing project structure if needed.
2. Use `run_snowflake_query` to inspect existing schemas and tables.
3. Generate a Python script that ingests the requested data.
4. Use `write_file` to save the script to `pipelines/<pipeline_name>/main.py`.
5. Use `write_file` to save `pipelines/<pipeline_name>/requirements.txt` with the
   pip packages your script imports — one per line, plain package specs only,
   no flags. Always include `snowflake-connector-python`.

## Destination database and schema

The active Snowflake connection is provided in the "Connection context" block
above this prompt. Use those values verbatim — do not invent or hardcode
alternatives. Specifically:

- Read `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_ROLE`, and
  `SNOWFLAKE_WAREHOUSE` from `os.environ` in your script.
- Do **not** set Python defaults for these (no `os.environ.get(..., "RAW_DB")`).
  If a variable is missing the script should fail loudly — Carve's runner
  guarantees they're set when invoking via `carve apply`.
- If the user's goal mentions a specific database, schema, or table name, use
  that. Otherwise default to the connection's database and schema.
- Create new tables (or new schemas, when justified) with `CREATE … IF NOT EXISTS`.

## Conventions

- Generated Python scripts go in `pipelines/<pipeline_name>/main.py`.
- Each pipeline has its own directory under `pipelines/`.
- Scripts use `snowflake-connector-python` for Snowflake access.
- Scripts read connection details from environment variables, not hardcoded
  values.
- Scripts are idempotent — running them twice should not corrupt data.

## Final response

After writing the script, respond with a brief summary covering:

- What the pipeline does, in one or two sentences.
- The destination — fully-qualified `<database>.<schema>.<table>` based on the
  connection context.
- The columns or shape of the loaded data, briefly.
- "Run with `carve apply <plan_id>`."

**Do not** include installation instructions, manual `export` commands, or
`python pipelines/...` invocations. The user will run the pipeline via
`carve apply`, which sets up the venv and injects environment variables. Saying
otherwise contradicts the actual workflow.
```

The exact wording is the engineer's call — these are the load-bearing constraints.

### Connection-context preamble

`src/carve/cli/orchestrator/planner.py`:

The planner currently passes the raw prompt body as `system=`. After this spec, it builds the system prompt as:

```
Connection context:
- Target: {target}
- Database: {db}
- Schema: {schema}
- Role: {role}
- Warehouse: {warehouse}

{prompt_body_from_disk}
```

When the default target has no Snowflake connection configured, omit the block entirely (the existing tool-stub path already covers that case).

Add a small helper, `_build_system_prompt(config: Config) -> str`, that's unit-testable in isolation.

### Postprocessor (safety net)

`src/carve/cli/orchestrator/planner.py`:

After the agent loop returns, the final assistant text becomes the plan summary. Add a `_clean_summary(text: str) -> str` that:

- Strips any `## How to Run` (and `## Setup`, `## Installation`) sections — anchor on a markdown header line, drop everything until the next `## ` header or end-of-document.
- Strips lines containing literal `pip install -r `, `pip install snowflake`, `export SNOWFLAKE_`, `python pipelines/`, `python -m`. (Be precise — don't accidentally strip prose that mentions these.)
- Logs a debug line when stripping happens, so the user/dev can see the prompt-rule violation.

The postprocessor is a backstop, not the primary defense. The prompt is the primary defense.

## Tests

`tests/cli/orchestrator/test_planner.py` — extend:

- `test_system_prompt_includes_connection_context` — config with a default target → built prompt starts with `Connection context:` and contains the target's database/schema/role/warehouse.
- `test_system_prompt_omits_context_when_no_target_configured` — no Snowflake connection → the prompt is just the file body, no "Connection context" header.
- `test_clean_summary_strips_how_to_run_section` — input contains `## How to Run\n...`; output has the section removed and the prose above it preserved.
- `test_clean_summary_strips_pip_install_lines` — input contains `pip install -r ...`; that line is removed, surrounding lines preserved.
- `test_clean_summary_preserves_carve_apply_line` — `Run with carve apply <plan_id>` lines pass through untouched.

`tests/core/agents/test_loop.py` — no changes needed; the loop is prompt-agnostic.

Update one of the existing happy-path planner tests to assert that the persisted `summary` field equals the cleaned text, not the raw model output.

## Acceptance criteria

- A new `carve plan` against a fresh project never picks a Snowflake database the user didn't configure — destination always derives from the connection or the user's explicit prompt.
- The plan summary never contains `pip install`, `export SNOWFLAKE_`, or `python pipelines/...` instructions.
- The plan summary always names the destination as `<database>.<schema>.<table>`.
- The connection context block appears in the system prompt when a default target is configured, and is absent when one isn't.
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover both the preamble and the postprocessor.
- Short `## [Unreleased]` note in `CHANGELOG.md`.

## Files this spec produces

Modified:

- `src/carve/core/agents/prompts/m1_code_agent.md` (restructured prompt)
- `src/carve/cli/orchestrator/planner.py` (preamble + cleaner)
- `tests/cli/orchestrator/test_planner.py`
- `CHANGELOG.md`

No new files.

## What this enables

- `carve plan` summaries become trustworthy — they describe what `carve apply` will actually do, not a parallel manual workflow.
- The agent stops inventing destination defaults, removing a class of "why is my data in `RAW_US_CENSUS`?" support questions.
- The Connection-context preamble pattern is the right shape for M2's specialist agents to inherit (each specialist gets its own preamble for its own concerns).
