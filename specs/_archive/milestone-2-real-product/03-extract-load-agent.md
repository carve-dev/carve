# M2-03 — Extract-load agent

**Milestone:** 2 — Real product
**Estimated effort:** ~1 day
**Dependencies:** M1-04 (agent loop), M1-06 (Snowflake connector), M2-09 (schema retrieval), M2-01 (plan/task-graph schema), M2-02 (orchestration agent — for routing)

## Purpose

The extract-load agent is the specialist that authors and modifies the Python "extract-and-load" layer of a pipeline: pulling rows from a source system (HTTP/REST, Socrata, S3/GCS, files, paginated DBs) and landing them in Snowflake. Its on-disk output is `pipelines/<pipeline_name>/main.py` plus `pipelines/<pipeline_name>/requirements.txt`.

This closes a gap left by M1.1-06. Today, `m1_build_agent` writes Python extract scripts as a generalist — its prompt covers connection rules, idempotency, and Snowflake binding all in one breath. As M2 introduces dbt and Snowflake specialists, the build agent reshapes into a **coordinator** that dispatches tasks to specialists. The Python extract-load layer needs its own specialist for the same reason dbt and Snowflake do: focused prompts produce tighter, more idiomatic code, and they make the recovery agent's `delegate_to_specialist` flow (M2-15) symmetric across layers.

## Responsibilities

**Source-side patterns:**

- HTTP/REST with offset, cursor, or `Link`-header pagination
- Retry with exponential backoff on transient failures (5xx, network, rate-limit)
- Socrata APIs via `sodapy` (the M1 smoke-test pattern)
- File reads from local paths, S3, or GCS (CSV, JSON, JSONL, Parquet)
- Paginated DB extracts (e.g. cursor-based selects against a source DB)

**Destination-side (Snowflake-only in M2):**

- Row-level loads via `executemany`
- DataFrame loads via `write_pandas`
- Bulk loads via `COPY INTO` from an internal stage
- `MERGE` upsert on a primary key
- Append-only inserts
- Watermark-driven incremental extraction (read max watermark from destination, pull source rows after it)

**Cross-cutting:**

- **Idempotency.** Re-running a generated pipeline is always safe — no duplicate rows, no errors. Matches M1.1-06's no-replay-guard model: the runner does not police idempotency; the script must.
- **Type coercion for JSON-ish nested data.** Snowflake's `executemany` rejects Python `dict` and `list` bindings; the agent stringifies them via `json.dumps` (or routes them through a `VARIANT` column when the schema supports it). This is the regression pattern surfaced by the Iowa-liquor smoke test and the M2-15 recovery scenario.
- **Connection wiring.** Reads `carve/connections.toml` via the env vars the runner injects (`SNOWFLAKE_*`, source-specific tokens). Never hardcodes credentials. Defaults are forbidden — `os.environ['X']`, not `os.environ.get('X', 'fallback')` — same rule M1.1-05 set for the build agent.
- **Structured logging.** `print(...)` lines that match the runner's expected format so progress streams render correctly through the M1.1-04 observer.
- **Error handling.** Surface partial failures (which page, which row range), use structured exceptions, support a dead-letter pattern when row-level errors are recoverable.
- **`requirements.txt` management.** Pin versions to a known-working set; keep the dependency list minimal (no `pandas` unless `write_pandas` is used; no `pyarrow` unless Parquet is involved).

## When the orchestrator invokes this agent

Routing patterns mirroring M2-02's two-layer selection:

- Goal mentions ingesting, extracting, or pulling data from an external source
- Goal references a source URL, REST endpoint, API, file path, bucket, or external DB
- Goal involves landing raw rows into Snowflake (i.e. work that happens *before* dbt models pick them up)
- The plan's `task_graph` contains a `Task` with `agent="extract_load"` (the canonical signal)

Excluded:

- Pure SQL transformation against existing tables → dbt agent (M2-04)
- DDL, RBAC, warehouse, or stage management → Snowflake agent (M2-05)
- Schema design for a single-pipeline ingest goal → orchestrator's existing `m1_plan_agent` shortcut still owns the **design** step; this agent only authors the resulting code at build time.

The single-pipeline-ingest happy path under M2-02 emits exactly one task with `agent="extract_load"`, replacing the M1.1 build agent's generalist role for that flow.

## Inputs (provided by the build-agent coordinator)

A `Task` object from the plan task graph (M2-01's `TaskGraph` schema):

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
        "convention_doc_excerpt": "<relevant slice of carve/conventions.md>",
        "existing_files": {  # populated for modify-existing-pipeline tasks; absent for new
            "main.py": "<current contents>",
            "requirements.txt": "<current contents>",
        },
    },
    "expected_outputs": [
        {"path": "pipelines/iowa_liquor_sales/main.py", "kind": "create", "preview": None},
        {"path": "pipelines/iowa_liquor_sales/requirements.txt", "kind": "create", "preview": None},
    ],
}
```

The coordinator pre-scopes this from the M2-01 `TaskGraph.tasks[i]` plus M2-02's per-task context assembly. The agent does not re-derive the design — that decision is fixed by plan time.

## Outputs

**File writes only.** No DB writes, no Snowflake DDL execution, no `pip install` during build. The agent's write tool is scoped to `pipelines/<pipeline_name>/`, and the only paths it should touch are `main.py` and `requirements.txt`.

The agent terminates by calling `submit_step(file_list, summary)`. The coordinator verifies the file list against the task's `expected_outputs` and dispatches the next task.

## Tools

Five tools, declared in `src/carve/core/agents/tools/extract_load_tools.py`:

1. `read_file(path)` — for reading existing `main.py` / `requirements.txt` when modifying a pipeline.
2. `write_file(path, content)` — scoped to `pipelines/<pipeline_name>/`. Refuses paths outside that directory (defense-in-depth alongside the project-root containment check from M1.1-06).
3. `lookup_skill(skill_name)` — loads the universal data-engineering skill or the Snowflake destination skill into the conversation on demand. Skills are *not* always-on (see "Skills" below).
4. `run_snowflake_query(sql, limit)` — read-only. Used sparingly to verify destination column types or check whether a target table already exists. The agent does not query source data — exploration happened at plan time.
5. `submit_step(file_list, summary, error=False)` — terminator tool. Mirrors `submit_plan`'s pattern from M1.1-06: `AgentLoop.terminator_tool="submit_step"` returns the loop's result immediately. Setting `error=True` signals the coordinator that the task is out of scope or otherwise unfixable from this agent's seat (e.g. "this is a dbt agent task").

The agent does **not** have:

- A tool to execute the script (only the runner does).
- A tool to run `pip install` (the runner manages venvs).
- A tool to write outside `pipelines/<pipeline_name>/`.

## Skills

Skills are markdown files loaded into the conversation via `lookup_skill(name)`. The agent decides which to load based on the task's `source.type` and `destination`. Loading is on-demand so the system prompt stays small for trivial tasks and only grows when a task warrants it.

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

### Skill loading model

Skills are *content* the agent appends to the conversation, not separate sub-agents. The `lookup_skill` tool returns the markdown body. The system prompt advertises the available skill names with one-line descriptions; the agent decides whether to load them. This keeps the always-on system prompt under ~2k tokens for simple tasks and only inflates when a task genuinely needs the detail.

## System prompt structure

`src/carve/core/agents/prompts/extract_load_agent.md`. Mirrors the dbt-agent prompt's organisation:

1. **Role.** "You are Carve's Python extract-load specialist. You author and modify the Python script that pulls rows from a source system and lands them in Snowflake."
2. **Connection-context preamble.** Target / database / schema / role / warehouse from `carve/connections.toml` (same pattern as M1.1-05 and `m1_build_agent`).
3. **Convention preamble.** The relevant excerpt of `carve/conventions.md` from M2-08 — only the parts that apply to extract-load code (file layout, logging style, retry posture). The orchestrator pre-scopes this; the agent does not load the whole conventions doc.
4. **Available skills.** A one-line description of `data_engineering` and `snowflake_destination` skills, with guidance on when to load each.
5. **Hard rules.**
   - No Python defaults for `SNOWFLAKE_*` env vars (`os.environ['X']` only).
   - Pass `role=` explicitly to `snowflake.connector.connect(...)`.
   - Re-running the script must be idempotent given the chosen `transformation.strategy`.
   - No "How to Run" / "Usage" section in the response — the runner owns execution.
   - Do not change `destination.table`, `destination.primary_key`, or `transformation.strategy` without `submit_step(error=True)`. Those are design decisions; surface them back to the coordinator instead of silently overriding.
6. **Response format.** Brief summary of files written + `submit_step(file_list, summary)` call. The coordinator reads only the `submit_step` payload; the summary is for the user.

## Build-agent coordinator role

The build agent (currently `src/carve/core/agents/prompts/m1_build_agent.md`) reshapes into a **coordinator** that dispatches each plan task to its assigned specialist via a new `invoke_specialist(agent_name, task)` tool. The extract-load agent is one such specialist; the dbt and Snowflake agents are the others. Recovery agent (M2-15) reuses the same dispatch via its existing `delegate_to_specialist` tool.

Concretely:

> The coordinator iterates `plan.task_graph.tasks` in order. For each task, it calls `invoke_specialist(task.agent, task)`. The specialist runs the M1-04 agent loop with its own system prompt, tools, and skill set, runs to completion, writes files, and calls `submit_step`. The coordinator verifies the returned file list against the task's `expected_outputs`, then dispatches the next task. On a specialist's `submit_step(error=True)`, the coordinator surfaces the error to the orchestrator (and, in dev, to the recovery agent).

**Implementation of the coordinator itself lives in M2-01's `builder.py` updates** — this spec only defines the contract the extract-load specialist satisfies. See M2-01 for coordinator details and M2-15 for the recovery-agent delegation symmetry.

## Acceptance criteria

- The extract-load agent produces working `pipelines/<name>/main.py` plus `requirements.txt` for at least these source patterns: HTTP-paginated REST, Socrata via `sodapy`, S3 file read, local CSV file read.
- Generated scripts pass the runner's structured-log format expectations (M1.1-04 observer parses progress lines correctly).
- Re-running a generated pipeline is idempotent under each documented `transformation.strategy` (`merge_upsert`, `truncate_load`, `append_only`, `watermark_incremental`).
- Type coercion for JSON-ish columns works without manual intervention — the Iowa-liquor `dict`-binding regression is covered by a fixture test.
- Skills are loaded on demand. Recorded LLM transcripts on simple tasks show no skill loaded; transcripts on Snowflake-MERGE-with-VARIANT tasks show both skills loaded.
- The agent rejects out-of-scope tasks via `submit_step(error=True, summary=...)`. A test fixture asserts this for a "transform stg_orders" goal mis-routed to extract-load.
- Generated `requirements.txt` is minimal (no `pandas` unless `write_pandas` is used; no `pyarrow` unless Parquet is involved) and pins to known-working versions.
- `ruff` + `mypy --strict` + full `pytest` stay green.

## Tests

- `tests/core/agents/test_extract_load_agent.py` — fixture-based unit tests with recorded LLM responses (Anthropic test-mode harness). Covers each of the four source patterns and each of the four transformation strategies.
- `tests/core/agents/test_extract_load_agent.py::test_rejects_out_of_scope` — asserts `submit_step(error=True)` for a dbt-shaped goal.
- `tests/core/agents/test_extract_load_agent.py::test_dict_binding_regression` — replays the Iowa-liquor failure context; expected output stringifies the dict columns.
- `tests/core/skills/test_data_engineering.py` — skill markdown parses, sub-section anchors are stable for `lookup_skill` to address.
- `tests/core/skills/test_snowflake_destination.py` — same for the Snowflake destination skill.
- `tests/integration/test_extract_load_flow.py` — invoke the agent against a stubbed plan task end-to-end; verify file output lands under `pipelines/<name>/`, verify `submit_step` payload matches `expected_outputs`.
- `tests/core/agents/test_extract_load_agent.py::test_skill_loading_is_on_demand` — for a simple task, asserts `lookup_skill` is not called; for a complex task, asserts it is.

## Files this spec produces

New:

- `src/carve/core/agents/extract_load/__init__.py`
- `src/carve/core/agents/extract_load/agent.py` — agent module (factory + run helper, mirroring the dbt/snowflake agent layout from M2-05 and M2-06).
- `src/carve/core/agents/tools/extract_load_tools.py` — the five tools listed above.
- `src/carve/core/agents/prompts/extract_load_agent.md` — system prompt.
- `src/carve/skills/__init__.py` — skill registry (if not already created by an earlier M2 spec; check M2-09 layout).
- `src/carve/skills/data_engineering.md` — universal data-engineering skill.
- `src/carve/skills/snowflake_destination.md` — Snowflake destination skill.
- `tests/core/agents/test_extract_load_agent.py`
- `tests/core/skills/test_data_engineering.py`
- `tests/core/skills/test_snowflake_destination.py`
- `tests/integration/test_extract_load_flow.py`

Modified:

- `src/carve/core/agents/__init__.py` — registry entry exposing the extract-load agent under the name `extract_load` for the coordinator's `invoke_specialist` lookup.

Cross-referenced (not edited here; changes happen in their owning specs):

- M2-02's orchestrator selection table — adds `extract_load` as a routable specialist.
- M2-01's `builder.py` — implements the coordinator that dispatches to this specialist.
- M2-15's recovery agent — adds `extract_load` to the `delegate_to_specialist` dispatch table.
- `src/carve/core/agents/prompts/m1_build_agent.md` — reshaped into the coordinator prompt by M2-01.

## Out of scope for M2

- Postgres, BigQuery, S3 (as a destination), DuckDB, or other destinations. Snowflake is the only supported destination in M2; other destinations land in M4 or via community contributions.
- Multi-step extract pipelines (extract → transform → load with intermediate staging files). Deferred to M3-01.
- Quality / test generation for the extracted data. M3 splits this from the dbt agent into a Quality agent; this agent does not author tests.
- Embedding-based source discovery ("find me an API that has Iowa liquor data"). M3.
- Streaming sources (Kafka, Kinesis, CDC). Deferred indefinitely; the architecture targets batch.
- Schema-drift recovery (column added or dropped at the source). M2-15 surfaces the diagnosis but does not auto-fix; this agent does not own that path.

## What this enables

- The single-pipeline-ingest happy path (M1.1-06's flow, now mediated by the M2-02 orchestrator) gets a focused specialist instead of a generalist build agent. Generated code is more idiomatic and the prompt is easier to evolve in isolation.
- The recovery agent's `delegate_to_specialist` flow becomes symmetric across layers — extract-load, dbt, Snowflake all reachable through the same dispatch.
- The dbt agent stays focused on SQL modeling, not on Python ingestion patterns it would otherwise drift into.
- Future extract patterns (new pagination styles, new source SDKs) ship as additions to the data-engineering skill rather than rewrites of a monolithic build prompt.

## Cross-references

- **M2-01** — Plan/task-graph schema this agent consumes; coordinator implementation.
- **M2-02** — Plan-time routing; how the orchestrator decides to invoke this agent.
- **M2-09** — Schema retrieval skills this agent leans on for destination column verification.
- **M2-15** — Recovery-agent delegation pattern; symmetric `delegate_to_specialist` dispatch.
- **M1.1-06** — Pipeline-centric lifecycle; `m1_build_agent` reshape into coordinator.
- **M1.1-05** — Connection-context preamble pattern reused in the system prompt.
- **ARCHITECTURE.md §7.1** — dev/prod target model; this agent's output is target-agnostic by construction.
