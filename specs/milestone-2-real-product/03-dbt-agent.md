# M2-03 — dbt agent

**Milestone:** 2 — Real product
**Estimated effort:** 1.5 days
**Dependencies:** M1-04 (agent loop), M2-05 (dbt integration), M2-08 (schema retrieval)

## Purpose

The dbt agent is the specialist that authors, modifies, and refactors dbt models, tests, and documentation. It's the most-used specialist in Carve because most data work flows through dbt.

## Responsibilities

- Generate new dbt models (staging, intermediate, mart)
- Modify existing models (refactor, change materialization, add columns)
- Generate or update `schema.yml` test definitions
- Generate or update model documentation strings
- Refactor SQL for readability or performance
- Suggest model splits when complexity warrants

In M2, the dbt agent also covers basic test generation. M3 splits dedicated test work into a separate Quality agent.

## Inputs (provided by orchestrator)

```python
{
    "goal": "make stg_orders incremental",
    "scope": "Modify the existing stg_orders model to use incremental materialization with order_id as unique key.",
    "context": {
        "convention_doc": "<contents of carve/conventions.md>",
        "affected_models": [
            {
                "name": "stg_orders",
                "path": "models/staging/stg_orders.sql",
                "sql": "<current SQL>",
                "schema_yml_entry": "<yaml fragment>",
                "materialization": "view",
                "downstream": ["int_orders_enriched", "fct_revenue"],
            }
        ],
        "source_columns": {
            "raw.orders": ["order_id", "customer_id", "amount", "created_at", "updated_at"]
        },
        "project_yml": {...}  # relevant fragments
    }
}
```

## System prompt

`src/carve/core/agents/prompts/dbt_agent.md`:

```markdown
You are Carve's dbt specialist. You author and modify dbt models with rigorous
attention to the project's existing conventions.

You will be given:
- A specific goal scoped to dbt work
- The project's conventions document
- The current state of any affected models
- Downstream dependencies that may be impacted
- Available source columns

Your output must:
- Match the project's conventions exactly (naming, materialization defaults, ref/source style, indentation, casing)
- Preserve downstream compatibility unless the goal explicitly requires breaking it
- Include schema.yml updates when adding or modifying columns
- Include or update doc strings (description fields) for new artifacts
- Use ref() and source() correctly — never hardcode table names
- Be idempotent: running dbt twice should produce the same result

If the goal is ambiguous, prefer the safer interpretation. Surface ambiguity in
your final summary so the user can refine.

Tools available:
- read_file: read any file in the project
- write_file: write a file in the project (use for new and modified models)
- run_dbt_command: run dbt commands like `dbt parse`, `dbt compile`, `dbt run --select <model>`
- query_dbt_manifest: structured queries against the manifest (downstream of, columns of)
- run_snowflake_query: read-only Snowflake queries to inspect source data

After completing the task, return a summary that includes:
- What changed (files created/modified)
- Why each decision was made (conventions followed, trade-offs)
- What downstream impact you verified
- Any ambiguity the user should review
```

## Tool set for the dbt agent

Six tools, declared in `src/carve/core/agents/tools/dbt_tools.py`:

1. `read_file(path)` — file reads scoped to project dir
2. `write_file(path, content)` — file writes scoped to project dir
3. `run_dbt_command(command, args)` — invoke `dbt` via `DbtRunner`
4. `query_dbt_manifest(query_type, params)` — structured manifest queries
5. `run_snowflake_query(sql, limit)` — read-only Snowflake
6. `list_files(path, pattern)` — glob within project

The `query_dbt_manifest` skill takes a query type:

- `model_by_name(name)` → full model metadata
- `downstream_of(model)` → list of dependent models
- `upstream_of(model)` → list of source dependencies
- `columns_of(model)` → list of declared columns
- `tests_on(model)` → list of tests on a model
- `models_in_path(path_glob)` → list of models matching a path pattern

Each query returns structured JSON (Pydantic-validated). The agent doesn't parse the manifest itself; it asks for what it wants.

## Common task patterns

### Pattern: modify an existing model

1. Read the current model SQL
2. Read the current `schema.yml` entry
3. Identify downstream models that reference modified columns
4. Generate the new SQL
5. Run `dbt compile --select <model>` to verify it parses
6. If applicable, run `dbt test --select <model>` to verify tests still pass
7. Write the file
8. Update `schema.yml` if columns changed
9. Summarize

### Pattern: create a new model

1. Look up the source columns to confirm they exist
2. Determine the appropriate path based on conventions (`staging/`, `intermediate/`, `marts/`)
3. Generate the SQL with appropriate `{{ ref() }}` or `{{ source() }}` calls
4. Generate the `schema.yml` entry with column-level docs and standard tests
5. Run `dbt parse` and `dbt compile --select <new_model>` to verify
6. Write both files
7. Summarize

### Pattern: refactor for readability

1. Read the current SQL
2. Identify substructure (CTEs that should be split, repeated logic)
3. Generate refactored SQL
4. Run `dbt compile` and verify the compiled SQL is identical (or semantically equivalent)
5. Write
6. Summarize, highlighting that behavior is unchanged

## Convention enforcement

The conventions document is included in the system prompt. The agent treats conventions as hard constraints unless the goal explicitly overrides them.

Examples of conventions the agent must respect:
- Model name prefixes: `stg_`, `int_`, `fct_`, `dim_`, `mart_`
- Always use `{{ ref() }}`, never hardcoded table names
- Ordering of CTEs (source → cleaned → final)
- Casing (snake_case columns, lowercase keywords)
- Whitespace and indentation
- Materialization defaults per directory

If a convention is unclear, the agent asks (in its summary, for follow-up) rather than guessing.

## Validation before write

Generated SQL is run through `dbt parse` (cheap) and `dbt compile --select <model>` (slightly more expensive) before being committed to a file. Failures are returned to the agent so it can correct.

This is a feedback loop the agent uses iteratively — three or four compile-fix cycles is normal for a first attempt at a new model.

## Output format

The dbt agent returns a structured result, not just text:

```python
class DbtAgentResult(BaseModel):
    summary: str
    files_created: list[FileChange]
    files_modified: list[FileChange]
    files_deleted: list[str]
    dbt_commands_run: list[str]
    ambiguities: list[str]  # things user should review
    downstream_impact: dict[str, str]  # model -> assessment
```

This structured result feeds the plan execution and the file-diff display in the UI.

## Tests

- Modifying a simple model produces correct SQL
- New model creation respects naming conventions
- Refactoring preserves compiled-SQL equivalence
- Schema.yml updates accompany column changes
- The agent surfaces ambiguity rather than guessing
- Compile failures trigger retries with corrections

Use a small test dbt project as a fixture (under `tests/fixtures/dbt-project/`).

## Acceptance criteria

- A goal targeting dbt work produces correct file changes
- Output respects the conventions document
- All written files pass `dbt parse`
- Downstream compatibility is verified (or breaking changes are surfaced)

## Files

- `src/carve/core/agents/dbt/__init__.py`
- `src/carve/core/agents/dbt/agent.py`
- `src/carve/core/agents/dbt/result.py`
- `src/carve/core/agents/tools/dbt_tools.py`
- `src/carve/core/agents/prompts/dbt_agent.md`
- `tests/core/agents/dbt/test_agent.py`
- `tests/fixtures/dbt-project/` (sample project)

## What this enables

- The most common Carve workflow (modify a dbt model) works end-to-end
- Convention inference (M2-07) has a direct consumer
- M3 quality agent splits cleanly from this one because tests are already isolated as a separate skill area
