# M3-15 — Pipeline lifecycle (disable / archive / restore / delete)

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 1 day
**Dependencies:** M2-01 (Build entity), M2-14 (deploy orchestration), M3-14 (step disable/enable — for the per-step pattern this generalizes)

## Purpose

In M2, pipelines accumulate but never go away. A pipeline lives in `pipelines/<name>/`, has a row in `pipelines`, and any deploys leave generated `.github/workflows/carve-deploy-<pipeline>-<target>.yml` files behind. Eventually a team needs to:

- **Disable** a pipeline so it stops running on schedule, but keep its history and code.
- **Archive** a pipeline so it disappears from the active list but stays queryable for audit.
- **Restore** an archived pipeline.
- **Delete** a pipeline outright (rare; usually wrong; offered for cleanup of test pipelines).

M2 kept this out of scope — the workflow file just sits there, the pipeline keeps running on schedule, and removal is a manual edit. M3 closes the gap.

## Scope (sketch — full spec lands when M3 starts)

### In scope

- `carve pipelines disable <name> [--target X]` — stops the scheduled execution for a target without removing the pipeline. Concretely: removes/disables the GitHub Actions workflow for that target. Pipeline row stays; `pipeline.deploy_state.<target>.status = "disabled"`. Re-enable with `enable`.
- `carve pipelines archive <name>` — sets `pipeline.archived_at`; hidden from default `carve pipelines` listing; queryable via `carve pipelines --include-archived`. Generated workflow files removed via a PR.
- `carve pipelines restore <name>` — clears `archived_at`. User decides whether to re-deploy to put workflow files back.
- `carve pipelines delete <name>` — destructive. Removes pipeline files via PR, removes pipeline row, removes generated workflow files. Refuses if the pipeline has run in the last 30 days unless `--force`.
- All four operations open a PR for the file-system changes; the DB-only changes happen locally.

### Out of scope (later)

- Bulk operations across many pipelines.
- Time-bounded archive (auto-restore after N days).
- Cross-target lifecycle (e.g., disable in dev but keep in prod). Per-target `disable` covers this; archive/restore/delete are pipeline-wide.
- Migration of pipeline data. Deleting a pipeline doesn't drop its destination table — that's a separate Snowflake DDL operation the user runs manually.

## Cross-references

- **M2-01** introduces the Pipeline + Build entities this spec extends with lifecycle status.
- **M2-14** generates the workflow files this spec disables/removes.
- **M3-14** disable/enable applies the same pattern at the step level; this spec generalizes it to whole pipelines.

## What this enables

- Teams can retire pipelines cleanly without manual cleanup.
- Audit history persists past the active life of a pipeline.
- Test/throwaway pipelines can be deleted without polluting the operational list.

---

*Stub planted during M2 spec review (2026-05-05). Full spec to be drafted at M3 kickoff.*
