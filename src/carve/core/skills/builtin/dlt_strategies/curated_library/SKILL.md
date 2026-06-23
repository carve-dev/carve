---
name: curated_library
description: >-
  Curated library copy strategy for dlt authoring. Apply when the goal matches a
  curated source pack with high confidence (dlt_library_match set) — copy the
  hand-tuned source from the curated library into the target component, customize
  it for the destination, and stamp library provenance. Highest in the strategy
  hierarchy: a curated source beats REST config, native dlt, and Singer.
---

# Curated library copy

**When this strategy applies.** Your context bundle carries `dlt_library_match`
set to a source name with `dlt_library_match_confidence = "high"` (e.g. the goal
explicitly names Stripe and a curated Stripe pack exists). A curated pack has
been hand-tuned for the source — it handles known quirks (rate limits, error
patterns, schema evolution) a from-scratch config wouldn't. This is the top of
the hierarchy: when a high-confidence curated match exists, prefer it over REST
config, native dlt, or Singer.

Confirm the match before copying: call `dlt_library` (`list` / `lookup`) and
trust the tool's confidence banding — do not re-derive a threshold. If the match
is only `medium` / `low` confidence, drop to the REST-config or native strategy
instead.

## Files this strategy produces

`dlt_library.copy(name, dest_path, customization)` lays the curated pack into
`el/<component_name>/` and writes, with the Carve provenance header:

- `el/<component_name>/__init__.py` — the curated source, customized.
- `el/<component_name>/requirements.txt` — pinned deps.
- additive entries in `.dlt/config.toml.template` / `.dlt/secrets.toml.template`
  for this destination + the source's credential references.

The provenance header on the copied `__init__.py` records `library_name`,
`library_commit`, and the destination customization. **Do not edit the header.**
After the copy, customize for the destination/schema with `edit` *below* the
header (resource selection, the destination name, the dataset/schema) — edits
below the header are preserved on regenerate.

## Provenance

Every file the copy writes carries the spec-03 provenance header (the
`dlt_library.copy` flow + `code_emitter` stamp it). For a curated copy it
additionally records `library_name` + `library_commit` so a later modification
can diff against the original pack.

## requirements.txt pinning

The copied `requirements.txt` pins dlt and its destination extra exactly — e.g.
`dlt[duckdb]==1.28.1` for the creds-free DuckDB substrate, `dlt[snowflake]==X.Y.Z`
for Snowflake — plus any source-specific extra the pack declares. Keep the pins
exact; never loosen them.

## Verification

After copying + customizing, **run it**: execute the component via Carve's venv
runner against the dev/test target (DuckDB is creds-free and the default proving
ground). `dlt` ships no `run`/`check` CLI and freeform `python` is gate-denied —
the venv runner is the structured execution path; `dlt pipeline <name> info` via
`bash` is for read-only inspection only. Read the parsed `CheckResult`
(rows-loaded / schema / errors) from the load package, confirm the loaded schema
via the `sql` tool, and iterate to green. A missing credential for an
authenticated source → `status = "needs_user_input"`, not a silent failure.
