---
name: native_dlt
description: >-
  Native dlt source strategy for dlt authoring. The fallback for complex sources
  the REST API config can't express — GraphQL, non-standard pagination, complex
  auth flows, streaming APIs, file-based sources, and database CDC (dlt's
  database-replication sources, NOT SaaS CDC). Write Python with @dlt.source and
  @dlt.resource, handling pagination, auth, and incremental cursors in code.
---

# Native dlt source

**When this strategy applies.** The source does not fit the REST API config
strategy: GraphQL, non-standard pagination, complex/multi-step auth, streaming
APIs, file-based sources, or database CDC. This is the most flexible strategy
and the fallback for complexity — but reach for REST config first whenever the
API is clean; native is more code and more surface area for mistakes.

**CDC scope.** "database CDC" here means dlt's database-replication sources
(e.g. Postgres `pg_replication`). SaaS CDC (e.g. Salesforce Change Data Capture)
is **not** in scope — dlt ships no SaaS CDC source; the curated SaaS sources are
cursor-based (e.g. `SystemModstamp`). Do not attempt SaaS CDC here.

## Files this strategy produces

- `el/<component_name>/__init__.py` — Python with `@dlt.source` + `@dlt.resource`
  decorators; pagination loops, auth, and incremental cursors
  (`dlt.sources.incremental`) handled in code. Carries the provenance header.
- `el/<component_name>/requirements.txt` — pinned deps (dlt + destination extra,
  plus any source-specific library, e.g. a GraphQL or DB driver).
- additive entries in `.dlt/config.toml.template` / `.dlt/secrets.toml.template`
  for the destination + credential references.

Author with `create_file` for the net-new module, then refine with `edit`. Match
patterns in the user's existing components (read via `existing_dlt_inspect` /
`grep`) so a new source is consistent with their authored ones.

## Provenance

The `__init__.py` carries the spec-03 provenance header via `code_emitter`. User
edits go below the header and are preserved on regenerate. Credentials are
`${ENV}` placeholders in `.dlt/secrets.toml.template` only — never literals.

## requirements.txt pinning

Pin dlt and its destination extra exactly — e.g. `dlt[duckdb]==1.28.1` for the
DuckDB substrate, `dlt[snowflake]==X.Y.Z` for Snowflake — plus any extra the
source needs (a GraphQL client, a DB driver). Keep pins exact, and adapt the
generated code to the project's pinned dlt version (avoid APIs not present in it).

## Verification

**Run it**: execute the component via Carve's venv runner against the dev/test
target (`dlt` ships no `run`/`check` CLI and freeform `python` is gate-denied —
the venv runner is the structured execution path; `dlt pipeline <name> info` via
`bash` is for read-only inspection only). Read the parsed `CheckResult`
(rows-loaded / schema / errors) from the load package, confirm the loaded schema
via the `sql` tool, and iterate to green. A bad incremental cursor field or a
pagination bug surfaces in the run — fix it against the real error, not a guess,
and re-run.
