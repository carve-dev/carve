---
name: rest_api_config
description: >-
  REST API generic config strategy for dlt authoring. Apply to clean REST APIs
  (JSON responses, standard pagination, bearer or OAuth auth) — emit a TOML
  config describing endpoints, pagination, and auth, plus a thin __init__.py that
  calls dlt.sources.rest_api.rest_api_source(...). Preferred over the native dlt
  strategy wherever it applies; the most lightweight option.
---

# dlt REST API generic config

**When this strategy applies.** The source is a clean REST API: JSON responses,
standard pagination (offset / page / cursor / link-header), and bearer or OAuth
auth. dlt's `rest_api_source` is a real production feature built exactly for
this — many SaaS APIs fit a config and need zero custom Python. Prefer it over
the native dlt strategy whenever the API fits; only fall back to native when the
API's quirks genuinely can't be expressed as config.

Probe an unfamiliar API with `rest_api_explore` (bounded: GET-only, request-
capped) and read its docs with `web_fetch` before settling the config shape.

## Files this strategy produces

- `el/<component_name>/__init__.py` — a thin module: loads the config and calls
  `dlt.sources.rest_api.rest_api_source(...)`. Carries the provenance header.
- `el/<component_name>/rest_api_config.toml` — the config (endpoints +
  pagination + auth), **or** inline in `__init__.py` for a simple API. Pick
  based on complexity; a multi-endpoint API reads better as a separate TOML.
- `el/<component_name>/requirements.txt` — pinned deps.
- additive entries in `.dlt/config.toml.template` / `.dlt/secrets.toml.template`
  for the destination + the API's credential references.

Author the `__init__.py` with `create_file`, then refine with `edit`
(read-before-edit, minimal diffs).

## Provenance

The `__init__.py` carries the spec-03 provenance header via `code_emitter`. Auth
credentials are `${ENV}` placeholders in `.dlt/secrets.toml.template` only —
never a literal key in the config or code.

## requirements.txt pinning

Pin dlt and its destination extra exactly — e.g. `dlt[duckdb]==1.28.1` for the
DuckDB substrate, `dlt[snowflake]==X.Y.Z` for Snowflake. `rest_api_source` ships
with dlt, so no extra dependency is usually needed beyond the destination extra.

## Verification

**Run it**: execute the component via Carve's venv runner against the dev/test
target (`dlt` ships no `run`/`check` CLI and freeform `python` is gate-denied —
the venv runner is the structured execution path; `dlt pipeline <name> info` via
`bash` is for read-only inspection only). Read the parsed `CheckResult` from the
load package, confirm the loaded schema via the `sql` tool, and iterate to green.
If the config paginates wrong or the auth fails, the run surfaces it — fix the
config and re-run; do not guess the response shape.
