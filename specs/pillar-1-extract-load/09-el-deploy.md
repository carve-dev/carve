# P1-09 — `carve el deploy` (lean, OSS-flexible)

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1 day
**Dependencies:** P1-01 (target system), P1-02 (plan/build lifecycle), P1-07 (Snowflake DDL for EL)
**Lineage:** Replaces the **parked M2-14 proposal** ([`specs/milestone-2-real-product/_spec_update_proposal_M2-14.md`](../milestone-2-real-product/_spec_update_proposal_M2-14.md), drafted but never accepted; see M2-14 review thread for context). The 5-phase ceremony and per-pipeline GHA workflow generation in that proposal are too prescriptive for OSS — they fight users on Airflow/GitLab/custom CI. This spec reframes deploy as **two phases** (local pre-flight + PR) plus **composable post-merge primitives** (`carve el provision`, `carve el verify`) the user wires into whatever CI/CD they already operate. Carries forward from the parked proposal: deploy-role / runtime-role separation pattern, branch naming `carve/<artifact>-<short_id>`, PR body template with deployment checklist, existing-PR detection + `--abandon-existing` flag, `Provider` abstraction.
**Status:** Stub. Full spec to be drafted.

## Purpose

Promote an EL artifact from one target to another (`--from dev --to prod`) by copying the artifact files into the destination target's folder, opening a PR, and exposing post-merge primitives the user wires into their own CI/CD. Carve generates the artifacts; the user owns the assembly.

## What this introduces

- **`carve el deploy <name> --from <X> --to <Y>`** runs locally and:
  1. **Pre-flight.** Resolves the source artifact (`targets/<X>/el/<name>/`) and the destination target (`targets/<Y>/`). Connects to target Y with the deploy role; verifies connectivity. Drift checks the destination schema against the build's expectations (recovery agent helps here, P1-10).
  2. **Copy.** Copies `targets/<X>/el/<name>/` into `targets/<Y>/el/<name>/`. Updates the Build row to record the deploy.
  3. **PR.** Branches, commits the copied files + the DDL file (`targets/<Y>/snowflake/<name>.sql`), pushes, opens a PR. PR body includes a deployment checklist: which DDL needs applying, which role applies it, which secrets the runtime workflow expects.
- **Composable post-merge primitives** (also runnable locally):
  - `carve el provision <name> --target Y` — applies `targets/<Y>/snowflake/<name>.sql` via the deploy role.
  - `carve el verify <name> --target Y` — checks the destination table exists with the expected columns; checks runtime-role grants.
  - (No separate `migrate` in Pillar 1; idempotent DDL covers the cases. Migration files arrive when Pillar 1 hits a real schema-evolution need.)
- **Example GitHub Actions workflow** at `docs/examples/github-actions-deploy.yml` (or similar). Calls the primitives in sequence. Users copy + adapt to their own CI/CD (GHA, GitLab CI, Airflow DAG, custom script) — Carve does **not** generate per-pipeline workflow files.
- **Existing-PR detection.** Subsequent `carve el deploy` for the same artifact + target pushes new commits to the open PR's branch by default; `--abandon-existing` closes the old PR and opens fresh.
- **Run-row recording.** Persists a `runs` row with `kind="deploy"`, `target=<Y>`, `target_id=<artifact_name>`, recording the PR url + head SHA. Phase 1 success ⇒ deploy `Run` is `"pr_opened"`; user-driven post-merge primitives update the row when they execute.

## What's deliberately out of scope (vs. the parked M2-14 proposal)

- **No generated workflow files.** Carve does not write `.github/workflows/carve-deploy-*.yml`. Users adopt the example from docs once and customize it for their own CI/CD setup. This is the OSS-flexible move; it stops Carve from fighting Airflow / GitLab / custom-script users.
- **No `--post-merge` hidden flag.** The post-merge phases are first-class CLI commands (`carve el provision`, `carve el verify`). The user — or their CI — invokes them directly.
- **No 5-phase ceremony.** Deploy is two phases: local pre-flight + PR. Provisioning and verification are separate user-driven commands. Cleanly composable.

## What carries forward from the M2-14 proposal

- Deploy role / runtime role separation (`[snowflake.<env>_deploy]` vs `[snowflake.<env>]`), as a documented recommended pattern.
- Branch naming (`carve/<artifact>-<short_id>`).
- PR body template with deployment checklist.
- Existing-PR detection + `--abandon-existing` flag.
- `Provider` abstraction so non-GitHub providers can be added later behind the same interface.

## Out of scope (deferred indefinitely or to later pillars)

- Multi-target deploy in one command (`--targets staging,prod`) — defer
- Cross-pillar deploy (deploying a pipeline that references multiple EL artifacts atomically) — Pillar 3
- AI-assisted recovery in CI (after the PR merges) — high complexity, defer to M3+ once we have telemetry on need
- GitLab / Bitbucket providers — same Provider interface; ship later
