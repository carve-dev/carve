# P1-07 — Snowflake DDL generation for EL destinations

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 0.5 day
**Dependencies:** M1-06 (Snowflake connector), P1-02 (plan/build lifecycle), P1-05 (extract-load agent)
**Lineage:** Narrow subset of **M2-05** ([`specs/milestone-2-real-product/05-snowflake-agent.md`](../milestone-2-real-product/05-snowflake-agent.md)). Pillar 1 ships only the per-EL DDL emission portion (the "Per-pipeline output" section added during the SDLC discussion). The full Snowflake agent's broader scope — warehouse creation/sizing, role hierarchies, account-level RBAC, dynamic tables, streams, tasks — defers to **Pillar 2** or later.
**Status:** Stub. Full spec to be drafted.

## Purpose

Generate the per-EL DDL that the deploy phase applies in the target's Snowflake account. For Pillar 1 this is narrow: the destination table for the EL artifact, the runtime-role grants needed to write to it, and any stage / file-format objects the script consumes. Lives at `targets/<target>/snowflake/<el-name>.sql` — committed to the repo, applied by `carve el provision` (P1-09).

## What this introduces

- **DDL emitter** invoked at build time alongside the EL Python authoring. The extract-load agent (P1-05) consults the catalog skill (P1-06) and emits a companion `<target>/snowflake/<el-name>.sql` covering:
  - `CREATE SCHEMA IF NOT EXISTS` for the destination schema (if Carve owns it).
  - `CREATE TABLE IF NOT EXISTS` for the destination table, with columns matching the script's writes.
  - `GRANT SELECT, INSERT, UPDATE, DELETE` on the destination to the runtime role.
  - Any internal stages or file formats the script uses (rare in Pillar 1).
- **Idempotency requirement.** All emitted DDL must be safe to re-run. `CREATE … IF NOT EXISTS`, `GRANT …` (idempotent on Snowflake).
- **Build manifest entry.** The Build row's `manifest_json` lists `snowflake/<el-name>.sql` so the deploy phase knows what to apply.

## Out of scope

- Full Snowflake agent (warehouses, network policies, role hierarchies, account-level operations) — Pillar 2 or later.
- Migration files (`migrations/NNN_slug.sql`) — defer to a later spec when we hit a real schema-evolution case; Pillar 1 ships idempotent `CREATE … IF NOT EXISTS` only.
- DROP / RENAME / non-idempotent DDL — defer indefinitely; the agent is constrained to additive idempotent operations in v0.1.
- Per-environment DDL divergence (different schemas for dev vs prod) — handled implicitly by per-target connection config; no special spec work.
