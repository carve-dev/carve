# P1-04 — Extract-load agent

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1 day
**Dependencies:** M1-04 (agent loop), M1-06 (Snowflake connector), P1-01 (target system), P1-02 (plan/build lifecycle), P1-05 (schema retrieval), P1-06 (Snowflake DDL for EL)
**Lineage:** Carries content from **accepted M2-03** ([`specs/milestone-2-real-product/03-extract-load-agent.md`](../milestone-2-real-product/03-extract-load-agent.md)) almost verbatim. The system prompt structure, tool set, both skills (`data_engineering.md`, `snowflake_destination.md`), the hard rules from **M1.1-05** (no `os.environ.get` defaults; pass `role=` explicitly; idempotency), and the regression test for the Iowa-liquor `dict`-binding bug all carry forward unchanged. **Net deltas:**
1. Output paths shift from `pipelines/<name>/` to `targets/<active_target>/el/<name>/` per the per-target folder model (P1-01).
2. Connection context comes from `carve/connections.toml`'s `[snowflake.<active>]` section (centralized model from P1-01), not `targets/<name>/connections.toml`.
3. The agent also emits the per-EL DDL companion file at `targets/<active>/snowflake/<name>.sql` (per P1-06).
4. **No coordinator wrapper** in Pillar 1 — the build flow (P1-02) invokes this specialist directly. The coordinator pattern from accepted M2-01's spec body is deferred until Pillar 2 brings additional specialists that warrant dispatch.

## Purpose

The extract-load agent is the specialist that authors and modifies the Python "extract-and-load" layer of a Pillar 1 artifact: pulling rows from a source system (HTTP/REST, Socrata, S3/GCS, files, paginated DBs) and landing them in Snowflake, plus emitting the per-EL DDL needed for the destination.

This is Pillar 1's **only** specialist. M1.1-06's `m1_build_agent` (which wrote scripts as a generalist) is reshaped into a thin shim that calls this specialist directly — the build coordinator pattern is deferred to Pillar 2 when a second specialist exists to dispatch to.

## Responsibilities

**Source-side patterns:**

- HTTP/REST with offset, cursor, or `Link`-header pagination
- Retry with exponential backoff on transient failures (5xx, network, rate-limit)
- Socrata APIs via `sodapy` (the M1 smoke-test pattern)
- File reads from local paths, S3, or GCS (CSV, JSON, JSONL, Parquet)
- Paginated DB extracts (e.g. cursor-based selects against a source DB)

**Destination-side (Snowflake-only in Pillar 1):**

- Row-level loads via `executemany`
- DataFrame loads via `write_pandas`
- Bulk loads via `COPY INTO` from an internal stage
- `MERGE` upsert on a primary key
- Append-only inserts
- Watermark-driven incremental extraction (read max watermark from destination, pull source rows after it)

**DDL emission (per P1-06):**

- Generates a companion `targets/<active>/snowflake/<artifact_name>.sql` with `CREATE SCHEMA IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, and the runtime-role `GRANT` statements the script needs at run time. Idempotent by construction; consumed at deploy time by P1-08's `carve el provision`.

**Cross-cutting:**

- **Idempotency.** Re-running a generated script is always safe — no duplicate rows, no errors. Matches M1.1-06's no-replay-guard model: the runner does not police idempotency; the script must.
- **Type coercion for JSON-ish nested data.** Snowflake's `executemany` rejects Python `dict` and `list` bindings; the agent stringifies them via `json.dumps` (or routes them through a `VARIANT` column when the schema supports it). This is the regression pattern surfaced by the Iowa-liquor smoke test and the recovery scenario in P1-09.
- **Connection wiring.** Reads `carve/connections.toml`'s `[snowflake.<active>]` section via the env vars the runner injects (target-prefixed `<TARGET>_SNOWFLAKE_*` and source-specific tokens). Never hardcodes credentials. Defaults are forbidden — `os.environ['X']`, not `os.environ.get('X', 'fallback')` — same rule M1.1-05 set for the build agent.
- **Structured logging.** `print(...)` lines that match the runner's expected format so progress streams render correctly through the M1.1-04 observer.
- **Error handling.** Surface partial failures (which page, which row range), use structured exceptions, support a dead-letter pattern when row-level errors are recoverable.
- **`requirements.txt` management.** Pin versions to a known-working set; keep the dependency list minimal (no `pandas` unless `write_pandas` is used; no `pyarrow` unless Parquet is involved).

## When the build flow invokes this agent

In Pillar 1 the build flow (P1-02) calls this agent **directly** — there's no orchestrator-level routing yet because Pillar 1 has only one specialist. The plan's `task_graph` always emits a single task with `agent="extract_load"`. The build flow looks at the task's `agent` field, validates it's `"extract_load"`, and invokes this agent with the task as input.

When Pillar 2 ships the dbt specialist (and possibly a separate snowflake DDL agent), the build flow gains a coordinator wrapper that dispatches by `task.agent`. P1-04's contract — "consume a task; produce files; call `submit_step`" — is designed so the coordinator wrap is mechanical (no agent-side rework).

Excluded scopes (deferred to later pillars; this agent surfaces them via `submit_step(error=True)`):

- Pure SQL transformation against existing tables → dbt agent (Pillar 2)
- Snowflake account-level operations (warehouses, role hierarchies, broad RBAC) → expanded Snowflake agent (Pillar 2 or later)
- Schema design — `m1_plan_agent` still owns the *design* step at plan time; this agent only authors the resulting code at build time.

## Inputs (provided by the build flow)

A `Task` object from the plan task graph (P1-02's `TaskGraph` schema):

```python
{
    "step": 1,
    "agent": "extract_load",
    "action": "generate_extractor",  # or "modify_extractor", "add_watermark", "fix_type_coercion"
    "inputs": {
        "goal": "Daily ingest of the most recent Iowa liquor sales rows.",
        "source": {
            "type": "socrata_api",
            "url": "https://data.iowa.gov/resource/m3tr-qhgy.csv",
            "row_limit": 10000,
            "ordering": "date DESC",
        },
        "destination": {
            "database": "<from connection context>",
            "schema": "<from connection context>",
            "table": "IOWA_LIQUOR_SALES",
            "primary_key": "INVOICE_LINE_NO",
        },
        "transformation": {
            "strategy": "merge_upsert",  # | "truncate_load" | "append_only" | "watermark_incremental"
            "rationale": "Bounded row count; MERGE on PK keeps re-runs idempotent.",
        },
        "columns": [
            {"name": "INVOICE_LINE_NO", "type": "VARCHAR(50)", "nullable": False},
            ...
        ],
        "convention_doc_excerpt": "<relevant slice of carve/conventions.md>",  # empty in Pillar 1; arrives with Pillar 2's M2-08 inference
        "existing_files": {  # populated for modify-existing-pipeline tasks; absent for new
            "main.py": "<current contents>",
            "requirements.txt": "<current contents>",
            "snowflake_sql": "<current contents of targets/<active>/snowflake/<name>.sql>",
        },
    },
    "expected_outputs": [
        {"path": "targets/dev/el/iowa_liquor_sales/main.py", "kind": "create", "preview": None},
        {"path": "targets/dev/el/iowa_liquor_sales/requirements.txt", "kind": "create", "preview": None},
        {"path": "targets/dev/snowflake/iowa_liquor_sales.sql", "kind": "create", "preview": None},
    ],
}
```

The build flow pre-scopes this from the M1.1-06 plan agent's `submit_plan(design)` payload plus the active target. The agent does not re-derive the design — that decision is fixed by plan time.

## Outputs

**File writes only.** No DB writes, no Snowflake DDL execution, no `pip install` during build. The agent's `write_file` tool is scoped to `targets/<active_target>/` (the build flow passes the resolved target into the tool factory) with allowed sub-paths constrained to:

- `el/<artifact_name>/main.py`
- `el/<artifact_name>/requirements.txt`
- `snowflake/<artifact_name>.sql`

Any other path raises an error. This is defense-in-depth alongside the project-root containment check from M1.1-06.

The agent terminates by calling `submit_step(file_list, summary)`. The build flow verifies the file list against the task's `expected_outputs` before recording the Build row (P1-02).

## Tools

Five tools, declared in `src/carve/core/agents/tools/extract_load_tools.py`:

1. `read_file(path)` — for reading existing `main.py` / `requirements.txt` / DDL when modifying an artifact.
2. `write_file(path, content)` — scoped to `targets/<active_target>/` with the path-allow-list above. Refuses paths outside.
3. `lookup_skill(skill_name)` — loads the universal data-engineering skill or the Snowflake destination skill into the conversation on demand. Skills are *not* always-on (see "Skills" below).
4. `run_snowflake_query(sql, limit)` — read-only against the active target's Snowflake (uses the runtime role from `[snowflake.<active>]`). Used sparingly to verify destination column types or check whether a target table already exists. The agent does not query source data — exploration happened at plan time.
5. `submit_step(file_list, summary, error=False)` — terminator tool. Mirrors `submit_plan`'s pattern from M1.1-06: `AgentLoop.terminator_tool="submit_step"` returns the loop's result immediately. Setting `error=True` signals the build flow that the task is out of scope or otherwise unfixable from this agent's seat (e.g. "this is a dbt agent task — out of scope for Pillar 1").

The agent does **not** have:

- A tool to execute the script (only `carve el run` / the runner does).
- A tool to run `pip install` (the runner manages venvs).
- A tool to write outside `targets/<active_target>/`.
- A tool to execute DDL against the target — provisioning is `carve el provision`'s job (P1-08), not the agent's.

## Skills

Skills are markdown files loaded into the conversation via `lookup_skill(name)`. The agent decides which to load based on the task's `source.type` and `transformation.strategy`. Loading is on-demand so the system prompt stays small for trivial tasks and only grows when a task warrants it.

### Universal data-engineering skill

`src/carve/skills/data_engineering.md`. Sub-sections:

- **Pagination patterns.** Offset, cursor, `Link`-header. Code stubs for each.
- **Retry with exponential backoff.** Idempotent retry on 5xx, network errors, rate-limit (429) responses. Honors `Retry-After` when present.
- **Watermark / incremental extraction.** Read max watermark from destination, pull source rows after it; commit watermark transactionally with the inserted rows.
- **Idempotent writes.** Decision tree for `MERGE` vs `DELETE+INSERT` vs append, keyed off whether the source has a stable PK and whether late-arriving rows update prior values.
- **Memory-bounded streaming.** Chunked reads from source (e.g. iterate API pages without buffering all rows), chunked writes to destination (`executemany` batches of 1k–10k rows).
- **Type coercion for JSON-ish nested data.** When the source returns nested `dict`/`list` values, either `json.dumps` them and bind as VARCHAR, or use a `VARIANT` column with `PARSE_JSON`. The Iowa-liquor pattern is the canonical example.
- **Structured logging.** Print format the runner expects: `print(f"[extract] page={i} rows={n}")`, `print(f"[load] inserted={n} table={t}")`. Match what M1.1-04's observer parses.
- **Connection management.** Read every credential from the environment with no Python defaults; pass `role=` explicitly to `snowflake.connector.connect(...)`; never rely on env auto-discovery (Carve disables it).

### Snowflake destination skill

`src/carve/skills/snowflake_destination.md`. Sub-sections:

- **`executemany` quirks.** Parameter binding, batch size, the `paramstyle='qmark'` requirement, the dict/list rejection.
- **`write_pandas`.** When to use it (medium-sized loads, schema inference helpful), schema implications, automatic table creation pitfalls.
- **`COPY INTO` from internal stage.** For large loads where row-by-row binding is too slow. PUT file → COPY → cleanup.
- **`MERGE` upsert pattern.** Canonical `USING (SELECT ... FROM VALUES ...)` form; explicit column lists.
- **Role / warehouse propagation.** The connection context's role and warehouse must reach the connection call; don't accept the connector's default.
- **Snowflake-specific types.** `VARIANT` for JSON-ish, `NUMBER(p,s)` scale handling, `TIMESTAMP_NTZ` vs `TIMESTAMP_TZ`, `DATE` vs `TIMESTAMP` for daily watermarks.
- **DDL emission patterns** (Pillar 1 specific, per P1-06). `CREATE TABLE IF NOT EXISTS` with the column list from the task; `GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE ... TO ROLE <runtime_role>`. Idempotent for safe re-application by `carve el provision`.

### Skill loading model

Skills are *content* the agent appends to the conversation, not separate sub-agents. The `lookup_skill` tool returns the markdown body. The system prompt advertises the available skill names with one-line descriptions; the agent decides whether to load them. This keeps the always-on system prompt under ~2k tokens for simple tasks and only inflates when a task genuinely needs the detail.

## System prompt structure

`src/carve/core/agents/prompts/extract_load_agent.md`:

1. **Role.** "You are Carve's Python extract-load specialist. You author and modify the Python script that pulls rows from a source system, lands them in Snowflake, and the companion DDL file that ensures the destination table exists with the right grants."
2. **Connection-context preamble.** Target / database / schema / role / warehouse from `carve/connections.toml`'s `[snowflake.<active>]` section (same pattern as M1.1-05 and `m1_build_agent`, just centralized).
3. **Convention preamble.** The relevant excerpt of `carve/conventions.md` from Pillar 2's M2-08 — only the parts that apply to extract-load code (file layout, logging style, retry posture). Pillar 1 ships with this empty unless a user has hand-written one; the build flow passes through what's present.
4. **Available skills.** A one-line description of `data_engineering` and `snowflake_destination` skills, with guidance on when to load each.
5. **Hard rules.**
   - No Python defaults for `<TARGET>_SNOWFLAKE_*` env vars (`os.environ['X']` only).
   - Pass `role=` explicitly to `snowflake.connector.connect(...)`.
   - Re-running the script must be idempotent given the chosen `transformation.strategy`.
   - DDL must be idempotent (`CREATE … IF NOT EXISTS`, `GRANT …`). Never `DROP` or `CREATE OR REPLACE` in Pillar 1.
   - No "How to Run" / "Usage" section in the response — the runner owns execution.
   - Do not change `destination.table`, `destination.primary_key`, or `transformation.strategy` without `submit_step(error=True)`. Those are design decisions; surface them back to the build flow instead of silently overriding.
6. **Response format.** Brief summary of files written + `submit_step(file_list, summary)` call. The build flow reads only the `submit_step` payload; the summary is for the user.

## Build flow integration (Pillar 1)

P1-02's `carve build <plan_id>` invokes this agent directly:

```python
# in src/carve/cli/orchestrator/builder.py (P1-02)
def build_pipeline(plan_id, active_target, config):
    plan = repo.get_plan(plan_id)
    task = plan.task_graph["tasks"][0]  # Pillar 1: always one task
    assert task["agent"] == "extract_load", "Pillar 1 supports only extract-load tasks"

    result = run_extract_load_agent(task, active_target, config)  # this spec
    verify_outputs(result.file_list, task["expected_outputs"])
    create_build_row(plan, result.file_list, active_target)
```

When Pillar 2 lands, this `assert` is replaced with a `dispatch_to_specialist(task, ...)` that knows about additional agents. P1-04 doesn't change — the build flow does.

## Acceptance criteria

- The extract-load agent produces working `targets/<active>/el/<name>/main.py` plus `requirements.txt` plus `targets/<active>/snowflake/<name>.sql` for at least these source patterns: HTTP-paginated REST, Socrata via `sodapy`, S3 file read, local CSV file read.
- Generated scripts pass the runner's structured-log format expectations (M1.1-04 observer parses progress lines correctly).
- Re-running a generated pipeline is idempotent under each documented `transformation.strategy` (`merge_upsert`, `truncate_load`, `append_only`, `watermark_incremental`).
- Generated DDL files are idempotent (`CREATE TABLE IF NOT EXISTS`, `GRANT …`); re-running `carve el provision` against an unchanged target is a no-op.
- Type coercion for JSON-ish columns works without manual intervention — the Iowa-liquor `dict`-binding regression is covered by a fixture test.
- Skills are loaded on demand. Recorded LLM transcripts on simple tasks show no skill loaded; transcripts on Snowflake-MERGE-with-VARIANT tasks show both skills loaded.
- The agent rejects out-of-scope tasks via `submit_step(error=True, summary=...)`. A test fixture asserts this for a "transform stg_orders" goal mis-routed to extract-load.
- Generated `requirements.txt` is minimal (no `pandas` unless `write_pandas` is used; no `pyarrow` unless Parquet is involved) and pins to known-working versions.
- Connection-context preamble correctly references target-prefixed env vars (`<TARGET>_SNOWFLAKE_*`) per the centralized model.
- `ruff` + `mypy --strict` + full `pytest` stay green.

## Tests

- `tests/core/agents/test_extract_load_agent.py` — fixture-based unit tests with recorded LLM responses (Anthropic test-mode harness). Covers each of the four source patterns and each of the four transformation strategies.
- `test_rejects_out_of_scope` — asserts `submit_step(error=True)` for a dbt-shaped goal.
- `test_dict_binding_regression` — replays the Iowa-liquor failure context; expected output stringifies the dict columns.
- `test_emits_ddl_companion_file` — verifies `targets/<target>/snowflake/<name>.sql` is in the file list with idempotent `CREATE TABLE IF NOT EXISTS` + `GRANT`.
- `test_skill_loading_is_on_demand` — for a simple task, asserts `lookup_skill` is not called; for a complex task, asserts it is.
- `test_write_file_path_allowlist` — agent attempting to write outside the three allowed paths raises an error.
- `test_uses_target_prefixed_env_vars` — generated script references `os.environ['DEV_SNOWFLAKE_USER']` (or whatever the active target's prefix is), not unprefixed.
- `tests/core/skills/test_data_engineering.py` — skill markdown parses, sub-section anchors are stable for `lookup_skill` to address.
- `tests/core/skills/test_snowflake_destination.py` — same for the Snowflake destination skill.
- `tests/integration/test_extract_load_flow.py` — invoke the agent against a stubbed plan task end-to-end via the build flow; verify file output lands under `targets/<active>/`, verify `submit_step` payload matches `expected_outputs`.

## Files this spec produces

New:

- `src/carve/core/agents/extract_load/__init__.py`
- `src/carve/core/agents/extract_load/agent.py` — agent module: `run_extract_load_agent(task, active_target, config) -> ExtractLoadResult`. Mirrors the agent layout that lands in Pillar 2 for dbt and snowflake.
- `src/carve/core/agents/tools/extract_load_tools.py` — the five tools listed above. `make_write_file_tool(allowed_paths: set[Path])` factory binds the path allow-list at call time.
- `src/carve/core/agents/prompts/extract_load_agent.md` — system prompt (per the structure above).
- `src/carve/skills/__init__.py` — skill registry (lookup_skill backing).
- `src/carve/skills/data_engineering.md` — universal data-engineering skill.
- `src/carve/skills/snowflake_destination.md` — Snowflake destination skill.
- `tests/core/agents/test_extract_load_agent.py`
- `tests/core/skills/test_data_engineering.py`
- `tests/core/skills/test_snowflake_destination.py`
- `tests/integration/test_extract_load_flow.py`

Modified:

- `src/carve/core/agents/__init__.py` — registry entry exposing the extract-load agent under the name `extract_load` for the build flow to look up.

Cross-referenced (not edited here; changes happen in their owning specs):

- P1-02's `builder.py` invokes `run_extract_load_agent(...)` from this spec.
- P1-06's DDL spec describes the contents and contract of `<target>/snowflake/<name>.sql`; this spec produces it.
- P1-09's recovery agent reuses `run_extract_load_agent` as its `delegate_to_specialist` target (the only one in Pillar 1).

## Out of scope for Pillar 1

- Postgres, BigQuery, S3 (as a destination), DuckDB, or other destinations. Snowflake is the only supported destination in Pillar 1; other destinations land in M4 or via community contributions.
- Multi-step extract pipelines (extract → transform → load with intermediate staging files). Deferred to Pillar 3.
- Quality / test generation for the extracted data. Pillar 2's dbt agent covers basic data tests; a dedicated Quality agent comes later.
- Embedding-based source discovery ("find me an API that has Iowa liquor data"). Far future.
- Streaming sources (Kafka, Kinesis, CDC). Deferred indefinitely; the architecture targets batch.
- Schema-drift recovery (column added or dropped at the source). P1-09 surfaces the diagnosis but does not auto-fix; this agent does not own that path.
- Coordinator dispatch pattern (`invoke_specialist`). Deferred to Pillar 2 when a second specialist exists.

## What this enables

- The Pillar 1 happy path (init → plan → build → run) runs through a focused specialist instead of a generalist build agent. Generated code is more idiomatic and the prompt is easier to evolve in isolation.
- The recovery agent's `delegate_to_specialist` flow (P1-09) has a real specialist to call for fix-and-retry attempts.
- Future extract patterns (new pagination styles, new source SDKs) ship as additions to the data-engineering skill rather than rewrites of a monolithic build prompt.
- When Pillar 2 brings the dbt agent, the build flow gains the coordinator wrapper and this spec's contract (`Task` in, `submit_step` out) plugs in unchanged.

## Cross-references

- **P1-01** — Target system; this agent reads from `[snowflake.<active>]` and writes under `targets/<active>/`.
- **P1-02** — Plan/Build lifecycle; defines the `TaskGraph` schema this agent consumes and the build flow that calls it.
- **P1-05** — Schema retrieval (catalog skills) this agent leans on for destination column verification.
- **P1-06** — Snowflake DDL contract; this agent produces the per-EL DDL companion file P1-06 specifies.
- **P1-09** — Recovery agent; uses this specialist as the primary `delegate_to_specialist` target for Pillar 1 fix attempts.
- **M1.1-06** — Pipeline-centric lifecycle; `m1_build_agent` becomes a thin shim that calls this specialist directly in Pillar 1.
- **M1.1-05** — Connection-context preamble pattern reused in the system prompt.
- **ARCHITECTURE.md §7.1** — dev/prod target model; this agent's output is target-aware (writes go under the active target's folder) but the script itself reads connection details at run time from whatever target is active then.
