# SQL: a dialect-aware tool layer + thin specialist

> Per [`../_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md): **SQL is a cross-cutting capability, not a silo.** A dialect-aware tool layer every subagent uses (the `sql` tool from [harness](./harness.md)) — `sqlglot` for transpile/validate, per-dialect `INFORMATION_SCHEMA` introspection, permission-gated execution — plus a thin **SQL specialist** for explain / write / modify. It also backs the `sql` step type (spec 08) and generalizes the shipped Snowflake-only `run_snowflake_query` + catalog skills.

## Status

- **Status:** Partially landed — lean core (Increment 2). The dialect-aware tool + DuckDB substrate shipped; the heavier parts are deferred (see below). This spec is the durable design *target*; the list below records the current gap.
- **Depends on:** [harness](./harness.md) (the `sql` tool + permission modes / role-scoped access), [layout](./layout.md) (connections per target), M1 Snowflake connector (HISTORICAL — generalized here).
- **Blocks:** the agents that query/author SQL (04 DLT, 08 pipeline + the `sql` step, 12 explorer, 17 recovery). Generalizes the M1 catalog skills + `run_snowflake_query`.
- **Landed (lean, Increment 2):** the `carve.core.sql` package — a `sqlglot` statement **classifier** (read/write/DDL/destructive, fail-closed; catches `WITH…INSERT`, `SELECT…INTO`, multi-statement), `validate`/`transpile`/`normalize_dialect`, dialect-dispatched `introspect` (Snowflake + DuckDB), and the **`sql` tool** (ops `validate`/`transpile`/`introspect`/`run`) bound to a connection's dialect + the active `PermissionMode`, with write/DDL enforced in-tool via the shipped `warehouse_roles` floor (deploy-only) and destructive-DDL approval. A first-class **DuckDB connector** (the local/test substrate) + a `[duckdb.<target>]` connection type. `sql` registered in the permission policy. A **dormant** `sql-specialist.md` builtin (routable once goal classification lands).
- **Deferred (not yet built; each tracked):** the **catalog-skill generalization** — the 5 `skills/builtin/catalog.py` skills and `run_snowflake_query` still run their original Snowflake path; `introspect.py` is the new parallel layer they migrate onto (the M1 paths are untouched, not broken). First-class **introspection adapters** for the four author-only dialects (postgres/bigquery/databricks/sqlserver — they validate/transpile today, but `introspect` raises). The live **orchestrator wiring** of the specialist (goal classification lands with plan-build). The `sql` **step type** (spec 08, Increment 3).

## Goal

One dialect-aware SQL capability, used everywhere:

1. **Validate / transpile / introspect / run** SQL, parameterized by the **connection's dialect** (snowflake, duckdb, postgres, bigquery, databricks, sqlserver). (Authoring — *explain / write / modify* — is the SQL specialist's LLM job, grounded by these tool ops; it is not a tool op.)
2. **`sqlglot`-backed** validation and transpile (author once, target the connection's dialect; catch dialect errors before execution — grounding for accuracy).
3. **Per-dialect introspection** (`INFORMATION_SCHEMA` / catalog) so agents read the *real* schema, never a guessed one.
4. **Permission-gated execution** (spec 15): reads on the **read role** in any mode; writes / DDL only in **`deploy`** mode on the **write role**, with approval required on destructive DDL. (Matches the shipped `warehouse_roles` floor, which denies warehouse writes below deploy; an earlier draft said "build" — reconciled to deploy.)
5. A thin **SQL specialist** subagent for "explain this query / write me this / make this incremental."

Carve ships **Snowflake + DuckDB first-class** (DuckDB powers local dev + tests); postgres/bigquery/databricks/sqlserver via `sqlglot` + adapter stubs, hardened later (matches the "Snowflake-tested, others via dlt/dbt natively" stance).

## Out of scope

- A Carve-owned SQL **execution engine** — we shell to the warehouse, we don't compute. The `sql` step stays "thin operational glue" (spec 08).
- **dbt model authoring** — the SQL specialist writes ad-hoc/step SQL, not dbt models.
- First-class hardening of bigquery/databricks/sqlserver (deferred; Snowflake + DuckDB are first-class).

## Behavior

### The `sql` tool (used by every agent)

`sql(op, *, connection, ...)` where `op ∈ {validate, transpile, introspect, run}` and the **dialect is resolved from `connection`** (the target's `connections.toml` entry):

- **`validate`** — `sqlglot.parse` against the dialect; returns parse errors *before* anything runs (grounding).
- **`transpile`** — rewrite SQL from one dialect to another (author once, target the connection's dialect).
- **`introspect`** — dialect-dispatched `INFORMATION_SCHEMA`/catalog reads (list/describe/exists), capped + `truncated` flags (reuses the skill-category caps).
- **`run`** — executes via the dialect adapter, **permission-gated** (below). Capped result rows; structured result.

*Authoring — explain / write / modify — is the SQL specialist's LLM job, not a tool op: the specialist writes SQL and grounds it with `validate`/`transpile`/`introspect` before `run`. The `sql` tool itself calls no LLM.*

### Permission-gated execution + role scoping

`execute.py` classifies each statement (read / write / DDL via `sqlglot`) and enforces the active **permission mode** (spec 15):

- `read_only` / `plan` / `build` / explorer → **read role**; reads only (`SELECT`/`SHOW`/`DESCRIBE`/`WITH`-select). Any write/DDL is **denied** below deploy (the shipped `warehouse_roles` floor; `role_for` raises).
- `deploy` → reads on the read role; writes/DDL on the **write role**, with **approval required** (interactive) / **deny** (headless) on `DROP`/`TRUNCATE`/destructive DDL.

(An earlier draft permitted warehouse writes in `build`; reconciled to deploy-only to match the shipped floor. Whether to relax to `build` is a future security-posture decision, revisited when the pipeline/dbt consumers land.)

The read-vs-write **role** comes from the target config (Carve already models deploy-vs-runtime roles); the layer never widens privileges.

### Dialects

- **Snowflake** — first-class (generalizes the M1 connector). **DuckDB** — first-class (local dev + the test substrate; lets the whole stack run without a warehouse).
- **postgres / bigquery / databricks / sqlserver** — `sqlglot` transpile/validate works now; introspection adapters are stubs hardened later. The agent can *author* dialect-correct SQL for all six today; *running* is fully tested on Snowflake + DuckDB.

### The SQL specialist (thin)

A declarative subagent (`sql-specialist.md`) for explicit SQL tasks — "explain this query," "write a query that …", "make this `sql` step incremental." Tools: `sql` (+ `edit` for `sql/*.sql` step files, `grep`). It's the delegate target when the orchestrator/recovery classifies a goal as SQL-authoring; most agents just call the `sql` *tool* directly without delegating.

## Tests

- **Unit (dialect):** `sqlglot` validates correct SQL and rejects malformed SQL pre-execution; `transpile` converts a query from one dialect to another (e.g. duckdb→snowflake); identifier quoting per dialect.
- **Unit (permission/roles):** a `SELECT` runs (read role) in every mode; a write/DDL is denied below deploy and runs on the write role at deploy; destructive DDL needs approval. A `SELECT … INTO` is classified a write (never dispatched to the read runner).
- **Integration (introspect):** `introspect` returns the real schema against a fixture **DuckDB** (and, later, a **Snowflake** testcontainer); caps + `truncated` honored; DuckDB results scoped to one catalog.
- **Unit (catalog generalization):** *deferred* — the 5 catalog skills + `run_snowflake_query` migrate onto `introspect.py` (the M1 paths are untouched until then).

## Acceptance

- Any agent can `validate`/`transpile`/`introspect`/`run` SQL against a connection, with the dialect resolved automatically and bad SQL caught before execution.
- Reads use the read role; writes/DDL are permission-gated (deploy-only) and use the write role; explorer/ask can never write.
- The full stack runs end-to-end on **DuckDB** locally (no warehouse needed); the other four dialects author valid SQL via `sqlglot`. (First-class Snowflake `run`/`introspect` reuses the shipped connector; a Snowflake integration test substrate is a follow-up.)
- *(Deferred)* The M1 Snowflake catalog skills + `run_snowflake_query` are generalized behind the dialect interface without breaking existing callers.

## Design notes

- **Why a shared SQL *tool* layer (not a SQL agent)?** Every agent touches SQL — dlt checks schemas, the pipeline engineer wires `sql` steps, recovery provisions a missing relation, the explorer traces lineage. Making SQL a *capability* (a tool, dialect-aware) used by all, plus a thin specialist for explicit authoring, avoids a silo and keeps each agent grounded in the real schema.
- **Why `sqlglot`?** Dialect-correct generation + validation + transpile, in one battle-tested library. It's the grounding that stops the agent shipping syntactically-wrong-for-this-warehouse SQL — and lets "write once, target the connection's dialect" work.
- **Why DuckDB first-class alongside Snowflake?** It makes the entire harness runnable locally and in CI without a warehouse — the test substrate for the verification loop (spec 15) and a real "try Carve in 2 minutes" path.
- **Why role-scoped execution?** Reads and writes must use different warehouse privileges; the permission mode selects the role, so an explorer physically cannot mutate the warehouse.

## Open questions

- **Connections schema for non-Snowflake dialects.** *Implementation default.* Generalize `connections.toml` per dialect; Snowflake + DuckDB shapes first, others additive.
- **Result-size caps for `run`.** *Implementation default.* Reuse the skill-category caps (default 100/200 rows, `truncated` flag); large reads paginate.
- **How much the SQL specialist overlaps the dbt engineer.** *Cross-reference.* The specialist handles ad-hoc/step SQL; dbt models are the dbt engineer's. Keep the boundary explicit when dbt authoring lands.
