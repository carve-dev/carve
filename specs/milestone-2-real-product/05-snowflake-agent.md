# M2-05 — Snowflake agent

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M1-04 (agent loop), M1-06 (Snowflake connector)

## Purpose

The Snowflake agent specializes in Snowflake-specific work: DDL (databases, schemas, tables, views), RBAC (roles, grants), warehouse management, and account-level operations. It's invoked when the orchestrator detects work that lives below the dbt layer.

## Responsibilities

- Generate DDL for new objects (databases, schemas, tables, views, file formats, stages)
- Author or modify RBAC: roles, role hierarchies, grants
- Manage warehouses (create, resize, suspend, set auto-suspend)
- Inspect account state (current usage, query history) for diagnostic goals
- Integrate with Snowflake-managed features (dynamic tables, streams, tasks) when goals require

## When the orchestrator invokes this agent

- Goal mentions creating, dropping, or altering a Snowflake object
- Goal mentions roles, permissions, or grants
- Goal mentions warehouse sizing or scheduling
- Goal touches an object outside the scope of dbt (raw schemas, stages, file formats)
- Goal involves Snowflake-specific features (Snowpipe, dynamic tables)

If the work is purely modeling against existing tables, the dbt agent handles it without involving this one.

## Inputs from orchestrator

```python
{
    "goal": "create a new dev schema for my Salesforce work with appropriate role grants",
    "scope": "Create RAW.SALESFORCE_DEV schema and grant USAGE + SELECT to the CARVE_DEV role.",
    "context": {
        "convention_doc": "<conventions.md>",
        "current_role": "CARVE_DEV",
        "available_roles": ["ACCOUNTADMIN", "SYSADMIN", "CARVE_DEV", "CARVE_PROD", "ANALYST"],
        "warehouses": ["CARVE_WH", "ANALYTICS_WH"],
        "existing_databases": ["RAW", "STAGING", "ANALYTICS"],
    }
}
```

## System prompt

```markdown
You are Carve's Snowflake specialist. You manage Snowflake DDL, RBAC, and
warehouse configuration with rigorous attention to least-privilege principles.

You will be given:
- A specific Snowflake-scoped goal
- The project's conventions document
- Current role and available roles
- Snapshot of existing objects in scope

Your output must:
- Use IF NOT EXISTS / OR REPLACE conservatively (only when explicitly safe)
- Apply least-privilege grants — start narrow, widen if needed
- Match the project's naming conventions for schemas, tables, roles
- Verify object existence with SHOW or INFORMATION_SCHEMA queries before assuming
- Use functional roles (CARVE_DEV, CARVE_PROD), not the user's personal role
- Generate idempotent scripts where possible

Tools available:
- run_snowflake_query: read-only queries (SHOW, DESCRIBE, SELECT)
- write_file: write SQL files into snowflake/ directory
- read_file: read existing SQL files
- list_files: list files in a directory

Write generated DDL to .sql files under snowflake/ — do not execute it directly.
The plan/deploy workflow handles execution. The user must review SQL before run.

After completing, summarize:
- Files generated and what each contains
- Permission impact (who can do what)
- Any operations that need elevated privileges (e.g., ACCOUNTADMIN)
- Rollback notes (how to undo)
```

## Tool set

Four tools, in `src/carve/core/agents/tools/snowflake_tools.py`:

1. `run_snowflake_query(sql, limit)` — read-only inspection
2. `write_file(path, content)` — write to project dir
3. `read_file(path)` — read project files
4. `list_files(path, pattern)` — glob

The Snowflake agent **does not** have a tool to execute DDL directly. All generated DDL goes to `.sql` files. Execution happens via the plan/deploy workflow with explicit user approval.

This is a deliberate safety boundary. RBAC and DDL changes are high-impact and need review.

## Output organization

Generated SQL goes into a structured directory:

```
snowflake/
├── databases/
│   └── 001_raw_database.sql
├── schemas/
│   ├── 001_raw_salesforce_dev.sql
│   └── 002_raw_salesforce_prod.sql
├── roles/
│   ├── 001_carve_dev.sql
│   └── 002_carve_prod.sql
├── grants/
│   ├── 001_carve_dev_raw_grants.sql
│   └── 002_carve_prod_raw_grants.sql
└── warehouses/
    └── 001_carve_wh.sql
```

The numbered prefix orders the deploy sequence. Generated grants reference the role and schema files by structure.

### Per-pipeline output

In addition to the shared `snowflake/` tree, when the snowflake agent is invoked as part of a multi-task plan that also involves an extract-load or dbt step, it emits two pipeline-scoped artifacts that M2-14's deploy orchestrator consumes:

- **`snowflake/<pipeline>.sql`** — the consolidated DDL needed for *this pipeline's* destination (table CREATE, schema CREATE IF NOT EXISTS, grants the runtime role needs to write to the destination). M2-14's Phase 3 applies this file via the deploy role.
- **`pipelines/<pipeline>/migrations/NNN_slug.sql`** — one or more idempotent migration files when the agent's task involves data transformations the runtime role can't perform during normal `carve run` (backfills, dedupe, conditional ALTERs). Numbered, alphabetical, must be idempotent. M2-14's Phase 4 runs all migration files in order on every deploy.

Both file types must be **idempotent** — re-running is safe. The `snowflake/<pipeline>.sql` uses `CREATE TABLE IF NOT EXISTS` and `GRANT ... ON ... TO ROLE ...` (idempotent on Snowflake). Migrations use `IF NOT EXISTS`, conditional ALTERs, or `MERGE` patterns. See M2-14 §"Migrations contract" for the exact constraint.

## Common task patterns

### Pattern: create a new schema

1. Verify the target database exists
2. Verify the schema doesn't already exist
3. Generate `CREATE SCHEMA <db>.<schema>;` with appropriate comment
4. Generate grants for the relevant roles
5. Write to `snowflake/schemas/NNN_<schema_name>.sql`

### Pattern: create a role

1. Check if role already exists; refuse to overwrite without explicit goal language
2. Generate `CREATE ROLE <name>;`
3. Suggest a parent role hierarchy (e.g., `GRANT ROLE <name> TO ROLE SYSADMIN`)
4. Write file
5. Note that role creation requires SECURITYADMIN or higher

### Pattern: add grants

1. Inspect what's currently granted via `SHOW GRANTS TO ROLE <name>`
2. Compute the diff
3. Generate only the missing GRANT statements (not redundant ones)
4. Write file

### Pattern: warehouse adjustment

1. Inspect current `SHOW WAREHOUSES LIKE '<name>'`
2. Generate `ALTER WAREHOUSE` with the requested change
3. Note any cost implications (size up = ~2x credits per hour)

## Safety rails

The agent is biased toward conservatism. Specifically:

- Never generates DROP statements unless the goal explicitly says to drop
- Always generates `CREATE` not `CREATE OR REPLACE` for new objects
- Inserts comments in DDL (`COMMENT = '...'`) noting Carve's authorship
- Surfaces operations requiring elevated privileges so the user knows

Hard rules baked into the prompt and the deploy layer:

- Never generates `DROP DATABASE` (too destructive)
- Never generates grants to PUBLIC role
- Never modifies the role Carve itself runs under

These are belt-and-suspenders — surfaced in the prompt and validated post-generation.

## Validation

Before adding a generated SQL file to a plan:

- The file must parse via a SQL parser (`sqlglot` with the Snowflake dialect)
- Forbidden patterns (DROP DATABASE, GRANT TO PUBLIC) trigger a guardrail violation
- File path must match the structure conventions

Failures are returned to the agent for correction, same loop as the dbt agent.

## Tests

- Schema creation goal produces correct DDL in correct path
- Grant goal produces only missing grants (not redundant)
- DROP DATABASE attempt is blocked
- Warehouse goal includes a cost note

Tests use a fixture Snowflake state via mocked query responses.

## Acceptance criteria

- A goal targeting Snowflake DDL produces correct, idempotent SQL files
- Files land in the structured `snowflake/` directory
- Forbidden patterns are blocked
- Generated SQL parses cleanly

## Files

- `src/carve/core/agents/snowflake/__init__.py`
- `src/carve/core/agents/snowflake/agent.py`
- `src/carve/core/agents/snowflake/result.py`
- `src/carve/core/agents/tools/snowflake_tools.py`
- `src/carve/core/agents/prompts/snowflake_agent.md`
- `tests/core/agents/snowflake/test_agent.py`

## What this enables

- DDL and RBAC work flows through Carve like any other change
- Generated SQL is reviewable in PRs before execution
- The dbt agent stays focused on modeling work
