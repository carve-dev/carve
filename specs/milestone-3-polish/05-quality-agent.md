# M3-05 — Quality agent

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 0.5 day
**Dependencies:** M2-04 (dbt agent), M2-09 (skills)

## Purpose

Split test/quality work out of the dbt agent into a dedicated Quality agent. The dbt agent focuses on model authoring; the Quality agent focuses on tests, freshness checks, and anomaly detection rules. Cleaner separation of concerns, better prompts, easier maintenance.

## Why split now (and not in M2)

In M2, the dbt agent absorbed test generation because it was simpler and the quality work was a small fraction of the agent's job. By M3, real users start asking for quality-focused goals ("add freshness checks to all sources", "find tables without primary key tests"), and a dedicated agent does this better.

Splitting also opens the door to non-dbt quality tools (Soda, Great Expectations, custom anomaly detection) since the Quality agent isn't dbt-specific.

## Responsibilities

- Generate dbt schema tests (unique, not_null, accepted_values, relationships)
- Generate `dbt-utils` and `dbt-expectations` tests
- Generate custom singular tests (SQL files in `tests/`)
- Generate source freshness configurations
- Suggest anomaly detection rules for key metrics
- Audit existing tests for completeness ("which models have no PK test?")
- Add tests in response to specific failures

## Inputs from orchestrator

```python
{
    "goal": "add freshness checks to all sources from Salesforce",
    "scope": "Add source freshness with warn_after=24h, error_after=48h to all salesforce sources",
    "context": {
        "convention_doc": "<conventions.md>",
        "affected_sources": [
            {"name": "salesforce.opportunities", "current_freshness": null},
            {"name": "salesforce.accounts", "current_freshness": null},
        ],
        "existing_freshness_examples": [
            # patterns from other sources to match
            {"source": "stripe.charges", "freshness": {...}},
        ]
    }
}
```

## System prompt

`src/carve/core/agents/prompts/quality_agent.md`:

```markdown
You are Carve's quality specialist. You generate tests, freshness checks, and
anomaly detection rules for data assets. You write rigorous, focused tests
that catch real problems without producing noise.

You will be given:
- A specific quality-scoped goal
- The project's conventions document
- The affected models or sources
- Examples of existing tests in the project (to match style)

Your output must:
- Match the project's existing test style (test types used, severity levels, naming)
- Use generic dbt tests where possible (unique, not_null, etc.)
- Use dbt_utils or dbt_expectations only if the project already uses them
- Write singular tests as last resort, with descriptive names
- Set test severity appropriately (error vs warn)
- Add freshness with reasonable thresholds based on observed data update patterns

Avoid:
- Adding redundant tests (don't add not_null on a column that already has it)
- Tight thresholds that will cause noise
- Generic tests that don't match the team's existing patterns
- Suggesting Great Expectations or other non-dbt tools unless explicitly asked

Tools available:
- read_file, write_file, list_files
- query_dbt_manifest: lookup existing tests, columns, sources
- run_snowflake_query: inspect data to choose thresholds
```

## Tool set

Same six tools as the dbt agent — the Quality agent operates on the same files.

## Common task patterns

### Pattern: add tests for a model

1. Read the model SQL to understand columns and grain
2. Check existing schema.yml for tests already on the model
3. Generate appropriate tests:
   - PK test (unique + not_null on primary key)
   - Foreign key tests (relationships)
   - Categorical tests (accepted_values)
   - Range tests (custom singular if needed)
4. Update the schema.yml; preserve existing structure

### Pattern: add freshness to sources

1. List the source's existing freshness config (if any)
2. Inspect recent data to understand cadence (if `run_snowflake_query` available)
3. Generate freshness with appropriate thresholds:
   - For hourly sources: `warn_after = 2h`, `error_after = 6h`
   - For daily sources: `warn_after = 30h`, `error_after = 48h`
4. Update the source's `_sources.yml`

### Pattern: audit existing tests

1. Query the manifest for all models without primary key tests
2. Query for sources without freshness
3. Produce a report (markdown) listing gaps with severity recommendations
4. Optionally: generate the missing tests

This pattern is "investigation + optional action" — the orchestrator may invoke just the audit, or follow up with generation.

## Output

Quality agent results have a similar shape to dbt agent results, plus an audit field:

```python
class QualityAgentResult(BaseModel):
    summary: str
    files_modified: list[FileChange]
    files_created: list[FileChange]
    audit_findings: list[AuditFinding]  # for "audit" goals

class AuditFinding(BaseModel):
    severity: Literal["info", "warn", "error"]
    artifact: str  # model or source name
    finding: str   # human-readable description
    suggested_action: str
```

## Tests

- Adding freshness to sources produces the right yaml shape
- Test additions don't duplicate existing tests
- Audit reports list real gaps (using a fixture project with deliberate gaps)
- Test naming matches conventions

## Acceptance criteria

- Goals targeting tests/quality are routed to this agent (orchestrator update)
- Generated tests respect conventions
- Existing tests are not duplicated
- Audit reports produce useful findings

## Files

- `src/carve/core/agents/quality/__init__.py`
- `src/carve/core/agents/quality/agent.py`
- `src/carve/core/agents/quality/result.py`
- `src/carve/core/agents/prompts/quality_agent.md`
- Update: `src/carve/core/agents/orchestration/selector.py` (route to quality)
- `tests/core/agents/quality/test_agent.py`

## What this enables

- Test work has a focused agent with focused prompts
- The dbt agent stays cleanly focused on modeling
- Future expansion to non-dbt quality tools (Soda, Great Expectations) has a natural home
