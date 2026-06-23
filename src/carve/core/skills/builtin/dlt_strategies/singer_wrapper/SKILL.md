---
name: singer_wrapper
description: >-
  Singer/Airbyte wrapper strategy for dlt authoring. The last-resort fallback,
  used sparingly: when no curated pack, REST config, or native dlt source fits
  and a Singer tap exists for the source, write a thin Python wrapper that
  invokes the tap via dlt and add tap-<name> to requirements.txt. Lowest in the
  strategy hierarchy — steer away from it unless the others genuinely don't apply.
---

# Singer/Airbyte wrapper

**When this strategy applies.** None of the higher strategies fit — no curated
pack, the API isn't a clean REST fit for config, and native dlt would be
prohibitive — **and** a Singer tap exists for the source. This is the bottom of
the hierarchy. Used sparingly: a Singer tap is an external dependency with its
own runtime and catalog, so prefer curated / REST config / native whenever they
apply. If you find yourself here, double-check the higher strategies first.

## Files this strategy produces

- `el/<component_name>/__init__.py` — a thin wrapper invoking the Singer tap via
  dlt (`dlt.sources.singer_pipeline.singer_source(...)` or equivalent). Carries
  the provenance header.
- `el/<component_name>/requirements.txt` — pinned deps, **including** the tap as
  `tap-<name>==X.Y.Z` alongside dlt and its destination extra.
- additive entries in `.dlt/config.toml.template` / `.dlt/secrets.toml.template`
  for the destination, the tap's catalog/config, and credential references.

Author with `create_file`, then refine with `edit`.

## Provenance

The `__init__.py` carries the spec-03 provenance header via `code_emitter`. The
tap's credentials are `${ENV}` placeholders in `.dlt/secrets.toml.template` only
— never literals in code or the tap config.

## requirements.txt pinning

Pin dlt and its destination extra exactly — e.g. `dlt[duckdb]==1.28.1` for the
DuckDB substrate, `dlt[snowflake]==X.Y.Z` for Snowflake — **and** pin the tap
itself (`tap-<name>==X.Y.Z`). An unpinned tap is a moving runtime; pin it.

## Verification

**Run it**: execute the component via Carve's venv runner against the dev/test
target (`dlt` ships no `run`/`check` CLI and freeform `python` is gate-denied —
the venv runner is the structured execution path; `dlt pipeline <name> info` via
`bash` is for read-only inspection only). Read the parsed `CheckResult` from the
load package, confirm the loaded schema via the `sql` tool, and iterate to green.
Singer taps surface their own config/catalog errors on the first run — fix
against the real error and re-run; a missing tap credential →
`status = "needs_user_input"`, not a silent failure.
