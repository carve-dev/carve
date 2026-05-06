# P1-10 — Recovery agent

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1.5 days
**Dependencies:** M1.1-04 (progress observer), P1-02 (plan/build lifecycle), P1-05 (extract-load agent), P1-08 (`carve el run`), P1-09 (`carve el deploy`)
**Lineage:** Carries forward from **M2-15** ([`specs/milestone-2-real-product/15-recovery-agent.md`](../milestone-2-real-product/15-recovery-agent.md)) which evolved continuously during M2 review (see the "Two trigger contexts" section added during the SDLC discussion). The system prompt, do-not-auto-fix categories, bounded-budget config, `parent_run_id` linking, and `AgentObserver` integration from M1.1-04 all carry forward unchanged. Scope **narrows** to Pillar 1's trigger contexts: `carve el run` failures and `carve el deploy` Phase 1 failures. The `delegate_to_specialist` tool in Pillar 1 has only one specialist target (extract-load); it expands to multiple in Pillar 2+.
**Status:** Stub. Full spec to be drafted.

## Purpose

Auto-fix loop for two failure surfaces in Pillar 1: a failed `carve el run` (the script ran and crashed) and a failed `carve el deploy` Phase 1 (pre-flight detected fixable drift before any prod writes). In both cases the recovery agent reads the failure, diagnoses, applies a fix, and retries within a bounded budget.

## What this introduces

- **Recovery agent prompt** at `src/carve/core/agents/prompts/recovery_agent.md`. Tools: `read_file`, `write_file` (scoped to `targets/<active>/el/<name>/`), `read_run_logs`, `run_snowflake_query` (read-only), `delegate_to_specialist` (for now, just the extract-load specialist; expands in Pillar 2+), `request_replan` (hand back to the planner agent).
- **Two trigger contexts:**
  1. **`carve el run` failure** — the auto-fix wrapper around `LocalVenvRunner`. Reads the script + the failure logs; tries patch-and-retry (rewrite a line, fix a type-coercion bug, etc.) before bailing.
  2. **`carve el deploy` Phase 1 failure** — drift, missing destination column, missing runtime-role grant, etc. Recovery agent suggests rebuilding (refine plan), regenerating DDL, or surfacing the issue back to the user with a clear next step. Phase 1 has no prod writes yet, so AI experimentation is safe.
- **Bounded budget.** `max_fix_attempts` (default 3) + `fix_cost_cap_usd` (default $1.00 per run). CLI overrides: `--max-fix-attempts N`, `--no-auto-fix`. From `carve/runner.toml`.
- **Do-not-auto-fix categories** (matches M2-15): auth failures, permission failures, resource exhaustion, user-cancellation, repeated identical failure (loop detection).
- **Per-attempt visibility** via M1.1-04's `AgentObserver`. Each attempt shows diagnosis → action → outcome.
- **`parent_run_id` column** on `runs` so `carve runs --pipeline <name>` shows the recovery chain as a tree.

## Out of scope

- Auto-fix in CI (post-merge deploy phases) — needs CI auth, branch-write privilege, follow-up-PR mechanics; deferred to M3+
- Auto-fix for `carve build` failures (rare in practice; defer)
- Cross-run learning ("this pipeline failed the same way last week") — M3+
- Schema-drift recovery beyond what Phase 1 handles — Pillar 2+
- Recovery agent reading PR review comments to fix issues autonomously — far future
