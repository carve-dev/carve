# M2-07 — Convention inference

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M2-05 (manifest reader), M2-06 (brownfield onboarding)

## Purpose

Analyze an existing dbt project to infer the team's conventions — naming patterns, materializations, testing style, SQL style — and write a `carve/conventions.md` document that gets included in every agent's system prompt. This is what grounds Carve's output in the team's actual patterns instead of generic defaults.

## Why this matters

The dbt agent's quality depends on convention awareness. Generic dbt code is acceptable; code that matches a team's existing style is *trusted*. Inference closes the gap between "AI-generated" and "looks like our engineer wrote it."

The convention doc is also editable by humans — it's a markdown file the team can refine, append to, or restructure. Carve treats it as authoritative input, not as something to regenerate every time.

## What gets inferred

Five categories of convention:

### 1. Naming

- Model name prefixes by directory (`stg_` in staging, `int_` in intermediate, `fct_` and `dim_` in marts)
- Source naming patterns
- Test naming patterns

Inferred by looking at all model names and grouping by directory.

### 2. Materialization

- Default materialization per directory
- Specific materializations called out as patterns (e.g., "facts use incremental")

Inferred from `config.materialized` values in the manifest.

### 3. SQL style

- Indent size (2 vs 4)
- Casing of keywords (uppercase, lowercase, mixed)
- CTE structure conventions (always end with `final` CTE, etc.)
- Column listing style (one per line vs single line)
- Reference style preferences (`{{ ref('x') }}` vs `{{ ref("x") }}`)

Inferred by sampling a handful of model files and looking at patterns.

### 4. Testing

- Common tests applied to PKs
- Source freshness conventions
- Custom test patterns
- Whether tests use `data_tests:` or `tests:` (post-1.7 syntax)

Inferred from `schema.yml` files.

### 5. Documentation

- Whether models have descriptions
- Whether columns have descriptions
- Use of doc blocks (`{% docs ... %}`)

Inferred from `schema.yml` and `.md` files in the docs directory.

## Inference algorithm

`src/carve/core/dbt/conventions.py`:

```python
def infer_conventions(project_dir: Path, manifest: DbtManifest) -> ConventionDoc:
    return ConventionDoc(
        naming=infer_naming(manifest),
        materialization=infer_materialization(manifest),
        sql_style=infer_sql_style(project_dir),
        testing=infer_testing(manifest),
        documentation=infer_documentation(manifest),
    )

def infer_naming(manifest: DbtManifest) -> NamingConventions:
    by_dir = defaultdict(list)
    for model in manifest.all_models():
        by_dir[model.directory].append(model.name)

    patterns = {}
    for directory, names in by_dir.items():
        prefixes = [name.split("_")[0] + "_" for name in names if "_" in name]
        if len(set(prefixes)) == 1:
            patterns[directory] = prefixes[0]
        elif len(set(prefixes)) <= 3 and all(prefixes.count(p) > 1 for p in set(prefixes)):
            patterns[directory] = list(set(prefixes))

    return NamingConventions(directory_prefixes=patterns)
```

Most inference is statistical: find the dominant pattern, note exceptions if they're a minority but consistent.

For SQL style, the simplest heuristic is to sample 5-10 model files and check:

- Most common indent (2 vs 4 spaces, tabs)
- Whether `select`, `from`, `where` are uppercase or lowercase
- Whether models tend to end with a `select * from final` pattern

If a strong majority pattern exists, infer it. If patterns are mixed, note that and don't pretend a convention exists.

## The output document

`carve/conventions.md` produced by inference:

```markdown
# Conventions for <project_name>

This document describes conventions inferred from your existing dbt project.
Carve agents read this document to match your team's style.

You can edit, expand, or rewrite this file. Carve will respect your edits.

## Last inferred: 2026-01-15
## Models analyzed: 47
## Confidence: high (consistent patterns) | medium (mixed) | low (insufficient data)

---

## Naming

### Model name prefixes
- `models/staging/`: `stg_`
- `models/intermediate/`: `int_`
- `models/marts/finance/`: `fct_`, `dim_`
- `models/marts/marketing/`: `mart_`

### Source files
- One file per source system: `_<system>__sources.yml`

## Materialization

- `staging/`: views (default)
- `intermediate/`: ephemeral
- `marts/`: tables, except for fct_revenue (incremental on order_date)

## SQL style

- Indentation: 2 spaces
- Keywords: lowercase (`select`, `from`, `where`)
- Final CTE: always named `final`, with `select * from final` as the last statement
- Column listing: one per line, indented
- ref() style: single quotes, no spaces inside parentheses

## Testing

Standard tests on primary keys:
- `unique`
- `not_null`

Source freshness on:
- `salesforce` (warn after 24h, error after 48h)
- `stripe` (warn after 6h, error after 24h)

Custom tests:
- `dbt_utils.unique_combination_of_columns` used on bridge tables

## Documentation

- Every model has a `description` in schema.yml
- ~80% of columns have descriptions (room to improve)
- Doc blocks used for shared definitions in `models/_docs.md`

## Notes

- 3 models break the naming convention (legacy from a migration); flagged for follow-up
- Two materialization patterns coexist in marts/ (table vs incremental); incremental is preferred for tables > 1M rows
```

The document includes confidence levels and notes about exceptions so the team has a starting point for cleanup.

## Confidence scoring

Each section gets a confidence rating:

- **High** — pattern holds for >90% of artifacts
- **Medium** — pattern holds for 70-90%
- **Low** — pattern holds for 50-70% (probably not a real convention)
- **Insufficient data** — fewer than 5 examples to infer from

Low-confidence inferences are surfaced as notes ("Mixed patterns detected; treating X as the default but Y is also common"). The agent can still use them, but the user is invited to clarify.

## Refresh and updates

Conventions are inferred:

- On `carve init` for brownfield projects
- On explicit `carve conventions refresh` (CLI command)
- *Not* automatically on every run (the file is treated as authoritative once written)

If the project's conventions evolve, the user can either:
- Hand-edit `carve/conventions.md`
- Run `carve conventions refresh` to regenerate (with confirmation, since this overwrites their edits)
- Run `carve conventions diff` to see what would change

## Validation

After inference, the document is validated:

- All sections present
- Confidence levels are filled in
- Format is parseable markdown

If any section comes back empty (insufficient data), it's filled with a placeholder: "No clear pattern detected — please add manually if you have one."

## Tests

- Inference on a fixture project produces expected sections
- Mixed patterns are correctly flagged with low confidence
- Empty sections produce placeholders
- Refresh respects the confirmation prompt
- Existing manual edits are preserved across `--no-overwrite` runs

Fixture: a small dbt project with deliberately mixed conventions in some places, consistent in others.

## Acceptance criteria

- `carve init` (brownfield) produces a useful conventions doc
- Patterns from a real-world dbt project are correctly inferred
- The doc is human-readable and human-editable
- Refresh works and respects manual edits

## Files

- `src/carve/core/dbt/conventions.py`
- `src/carve/core/dbt/conventions_inference/naming.py`
- `src/carve/core/dbt/conventions_inference/materialization.py`
- `src/carve/core/dbt/conventions_inference/sql_style.py`
- `src/carve/core/dbt/conventions_inference/testing.py`
- `src/carve/core/dbt/conventions_inference/documentation.py`
- `src/carve/core/dbt/conventions_render.py`
- `src/carve/cli/commands/conventions.py`
- `tests/core/dbt/test_conventions_inference.py`

## What this enables

- The dbt agent's output matches team patterns
- Users feel that Carve "gets" their codebase from minute one
- A pathway exists for users to encode tribal knowledge that the agents will respect
