# P1-02 — Plan / Build lifecycle (per-target)

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1 day
**Dependencies:** M1-03 (state store), M1.1-06 (existing plan/build/run/deploy lifecycle), P1-01 (target system)
**Lineage:** Continues **M1.1-06** (plan/build/run/deploy verbs, `Pipeline` table, plan-as-design contract). Reuses **accepted M2-01** (Build entity, migration `0004_build_entity.py`, `Pipeline.current_plan_id` → `current_build_id`). Net-new in this spec: per-target output paths. The multi-task task graph and build-coordinator pattern from accepted M2-01 are explicitly **deferred to Pillar 2** (only meaningful with multiple specialists).
**Status:** Stub. Full spec to be drafted.

## Purpose

Adapt M1.1-06's plan → build → run → deploy lifecycle to the per-target folder model and introduce the **Build** entity as the durable, deployable unit. In Pillar 1 the lifecycle has one specialist (the extract-load agent), so the multi-task task graph and build-coordinator pattern from the earlier M2-01 design are deferred to Pillar 2.

## What this introduces

- **`Build` table.** Columns: `id` (build_<hex>), `pipeline_name` (the EL artifact name), `plan_id` (biographical), `target` (which target it was built against), `created_at`, `manifest_json` (DDL files + migration files this build expects), `commit_sha` / `pr_url` / `deployed_at` (set by deploy).
- **`Pipeline.current_plan_id` → `current_build_id`.** Migration `0004_build_entity.py` creates `builds` + renames the FK + drops `plans.estimates_json` (unused).
- **`carve plan "<goal>"`** stays general — the AI agent designs an EL artifact (Pillar 1's only specialist for now) via `submit_plan(design)`. Design content matches what the existing `m1_plan_agent` produces today.
- **`carve build <plan_id>`** invokes the extract-load specialist directly (no coordinator yet — single specialist). Writes files to `targets/<active_target>/el/<artifact_name>/`. Creates a new `Build` row pointing at the plan + target.
- **`Pipeline` row** continues to track per-artifact state at the dev-side state DB. Future pillars (Pipeline pillar, etc.) get their own collection or reuse this table — TBD when Pillar 2 starts.

## Out of scope

- Multi-task task graphs (Pillar 2+)
- Build coordinator pattern with `invoke_specialist` (Pillar 2+, when there are multiple specialists)
- `carve plan diff` (defer; nice-to-have)
- Cost / duration / Snowflake credit estimates (deferred indefinitely)
- Guardrail check, plan expiry, file-diff previews, config-hash validation (all deferred)
