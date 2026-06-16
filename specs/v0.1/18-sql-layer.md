# v0.1-18 — SQL: a dialect-aware tool layer + thin specialist

> Per [`../_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md): **SQL is a cross-cutting capability, not a silo.** A dialect-aware tool layer every subagent uses (the `sql` tool from [v0.1-15](./15-agent-harness.md)) — `sqlglot` for transpile/validate, per-dialect `INFORMATION_SCHEMA` introspection, permission-gated execution — plus a thin **SQL specialist** for explain / write / modify. It also backs the `sql` step type (spec 08) and generalizes the shipped Snowflake-only `run_snowflake_query` + catalog skills.

## Status

- **Status:** Drafting
- **Depends on:** [v0.1-15 agent-harness](./15-agent-harness.md) (the `sql` tool + permission modes / role-scoped access), [v0.1-03 flat-layout](./03-flat-layout.md) (connections per target), M1 Snowflake connector (HISTORICAL — generalized here).
- **Blocks:** the agents that query/author SQL (04 DLT, 08 pipeline + the `sql` step, 12 explorer, 17 recovery). Generalizes the M1 catalog skills + `run_snowflake_query`.

## Goal

One dialect-aware SQL capability, used everywhere:

1. **Explain / write / modify / validate / run** SQL, parameterized by the **connection's dialect** (snowflake, duckdb, postgres, bigquery, databricks, sqlserver).
2. **`sqlglot`-backed** generation, validation, and transpile (author once, target the connection's dialect; catch dialect errors before execution — grounding for accuracy).
3. **Per-dialect introspection** (`INFORMATION_SCHEMA` / catalog) so agents read the *real* schema, never a guessed one.
4. **Permission-gated execution** (spec 15): reads on the **read role**; writes / DDL only in `build`/`deploy` mode on the **write role**, with a prompt on destructive DDL.
5. A thin **SQL specialist** subagent for "explain this query / write me this / make this incremental."

v0.1 ships **Snowflake + DuckDB first-class** (DuckDB powers local dev + tests); postgres/bigquery/databricks/sqlserver via `sqlglot` + adapter stubs, hardened post-v0.1 (matches the "Snowflake-tested, others via dlt/dbt natively" stance).

## Out of scope

- A Carve-owned SQL **execution engine** — we shell to the warehouse, we don't compute. The `sql` step stays "thin operational glue" (spec 08).
- **dbt model authoring** (v0.2) — the SQL specialist writes ad-hoc/step SQL, not dbt models.
- First-class hardening of bigquery/databricks/sqlserver (post-v0.2; v0.1 is Snowflake + DuckDB).

## Files this spec produces

```
src/carve/core/sql/dialect.py            # NEW — sqlglot wrapper: parse, validate, transpile(target_dialect), lint, identifier-quote per dialect
src/carve/core/sql/introspect.py         # NEW — dialect-dispatched schema introspection (list_databases/schemas/tables, describe_table, table_exists)
src/carve/core/sql/execute.py            # NEW — permission-gated execution: classify read vs write/DDL, select role (read|write) from the permission mode, prompt on destructive DDL, capped results
src/carve/core/sql/adapters/snowflake.py # MODIFY/NEW — generalize the M1 Snowflake connector behind the dialect interface
src/carve/core/sql/adapters/duckdb.py    # NEW — local-dev + test dialect (first-class)
src/carve/core/sql/adapters/__init__.py  # NEW — adapter registry (postgres/bigquery/databricks/sqlserver = sqlglot + stub introspection, post-v0.1 hardening)
src/carve/core/agents/tools/sql.py       # NEW — the `sql` tool (spec 15): explain | generate | modify | validate | introspect | run, dialect from the connection
src/carve/core/agents/builtin/sql-specialist.md   # NEW — the thin SQL specialist declarative agent
src/carve/core/skills/builtin/catalog.py # MODIFY — the 5 catalog skills become dialect-dispatched (Snowflake + DuckDB), via introspect.py
src/carve/core/agents/m1_tools.py        # MODIFY — run_snowflake_query -> the dialect-aware `sql` run path (back-compat alias)
docs/sql-layer.md                        # NEW — dialects, role-scoping, the sql tool + specialist
tests/unit/test_sql_dialect.py           # NEW — sqlglot validate/transpile across dialects; bad SQL caught pre-exec
tests/unit/test_sql_permission_roles.py  # NEW — read on read-role; DDL prompts; write role only in build/deploy
tests/integration/test_sql_introspect_duckdb.py # NEW — real introspection against a fixture DuckDB
tests/integration/test_sql_introspect_snowflake.py # NEW — against a Snowflake fixture/testcontainer
```

## Behavior

### The `sql` tool (used by every agent)

`sql(op, *, connection, ...)` where `op ∈ {explain, generate, modify, validate, introspect, run}` and the **dialect is resolved from `connection`** (the target's `connections.toml` entry):

- **`validate`** — `sqlglot.parse` against the dialect; returns parse errors *before* anything runs (grounding).
- **`generate` / `modify`** — author/edit SQL; the agent's LLM writes it, `validate` gates it, `transpile` ensures dialect-correctness.
- **`introspect`** — dialect-dispatched `INFORMATION_SCHEMA`/catalog reads (list/describe/exists), capped + `truncated` flags (reuses the skill-category caps).
- **`run`** — executes via the dialect adapter, **permission-gated** (below). Capped result rows; structured result.
- **`explain`** — read + summarize a query/object (used by the explorer).

### Permission-gated execution + role scoping

`execute.py` classifies each statement (read / write / DDL via `sqlglot`) and enforces the active **permission mode** (spec 15):

- `read_only` / `plan` / explorer → **read role**; `SELECT`/`SHOW`/`DESCRIBE`/`WITH` only; any write/DDL is denied.
- `build` → reads on read role; writes within the agent's scope on the **write role**; **prompt** (interactive) / **deny** (headless) on `DROP`/`TRUNCATE`/destructive DDL.
- `deploy` → as build (the linked-PR flow opens PRs; warehouse writes still happen on the next run, not at deploy).

The read-vs-write **role** comes from the target config (Carve already models deploy-vs-runtime roles); the layer never widens privileges.

### Dialects

- **Snowflake** — first-class (generalizes the M1 connector). **DuckDB** — first-class (local dev + the test substrate; lets the whole stack run without a warehouse).
- **postgres / bigquery / databricks / sqlserver** — `sqlglot` transpile/validate works now; introspection adapters are stubs hardened post-v0.1. The agent can *author* dialect-correct SQL for all six today; *running* is fully tested on Snowflake + DuckDB.

### The SQL specialist (thin)

A declarative subagent (`sql-specialist.md`) for explicit SQL tasks — "explain this query," "write a query that …", "make this `sql` step incremental." Tools: `sql` (+ `edit` for `sql/*.sql` step files, `grep`). It's the delegate target when the orchestrator/recovery classifies a goal as SQL-authoring; most agents just call the `sql` *tool* directly without delegating.

## Tests

- **Unit (dialect):** `sqlglot` validates correct SQL and rejects malformed SQL pre-execution; `transpile` converts a query from one dialect to another (e.g. duckdb→snowflake); identifier quoting per dialect.
- **Unit (permission/roles):** a `SELECT` runs on the read role in `read_only`; a `DROP` is denied in `read_only` and prompts in `build`; writes use the write role only.
- **Integration (introspect):** `introspect` returns the real schema against a fixture **DuckDB** and a **Snowflake** testcontainer; caps + `truncated` honored.
- **Unit (catalog generalization):** the 5 catalog skills resolve through `introspect.py` for both Snowflake and DuckDB; `run_snowflake_query` still works via the back-compat alias.

## Acceptance

- Any agent can `validate`/`generate`/`introspect`/`run` SQL against a connection, with the dialect resolved automatically and bad SQL caught before execution.
- Reads use the read role; writes/DDL are permission-gated and use the write role; explorer/ask can never write.
- The full stack runs end-to-end on **DuckDB** locally (no warehouse needed) and on **Snowflake**; the other four dialects author valid SQL via `sqlglot`.
- The M1 Snowflake catalog skills + `run_snowflake_query` are generalized behind the dialect interface without breaking existing callers.

## Design notes

- **Why a shared SQL *tool* layer (not a SQL agent)?** Every agent touches SQL — dlt checks schemas, the pipeline engineer wires `sql` steps, recovery provisions a missing relation, the explorer traces lineage. Making SQL a *capability* (a tool, dialect-aware) used by all, plus a thin specialist for explicit authoring, avoids a silo and keeps each agent grounded in the real schema.
- **Why `sqlglot`?** Dialect-correct generation + validation + transpile, in one battle-tested library. It's the grounding that stops the agent shipping syntactically-wrong-for-this-warehouse SQL — and lets "write once, target the connection's dialect" work.
- **Why DuckDB first-class alongside Snowflake?** It makes the entire harness runnable locally and in CI without a warehouse — the test substrate for the verification loop (spec 15) and a real "try Carve in 2 minutes" path.
- **Why role-scoped execution?** Reads and writes must use different warehouse privileges; the permission mode selects the role, so an explorer physically cannot mutate the warehouse.

## Open questions

- **Connections schema for non-Snowflake dialects.** *Implementation default.* Generalize `connections.toml` per dialect; Snowflake + DuckDB shapes in v0.1, others additive.
- **Result-size caps for `run`.** *Implementation default.* Reuse the skill-category caps (default 100/200 rows, `truncated` flag); large reads paginate.
- **How much the SQL specialist overlaps the dbt engineer (v0.2).** *Cross-reference.* The specialist handles ad-hoc/step SQL; dbt models are the v0.2 dbt engineer's. Keep the boundary explicit when v0.2 lands.
