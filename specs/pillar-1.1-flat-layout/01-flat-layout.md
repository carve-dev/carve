# P1.1-01 — Flat artifact layout

**Milestone:** Pillar 1.1 — Flat layout + git-based promotion
**Estimated effort:** 1 day
**Dependencies:** P1-01 (target system), P1-02 (build/run lifecycle), P1-07 (`carve el run`)
**Lineage:** Replaces P1-01's per-target folder structure (`targets/<X>/el/<name>/`) with a single artifact tree (`el/<name>/`). The target system itself stays — `[snowflake.<target>]` sections in `connections.toml`, `<TARGET>_SNOWFLAKE_*` env vars, the `--target X` flag, and the `runs.target` / `builds.target` columns are all preserved.

## Purpose

Move EL artifacts from per-target subtrees to a single tree per artifact. This is the foundation that unlocks git-based promotion: one code copy answers "what's in production" (via git commits / tags), and the same code runs against any target by switching env-var prefixes at runtime.

## What changes

**Before (Pillar 1):**
```
targets/
  dev/el/iowa/main.py
  dev/el/iowa/requirements.txt
  dev/el/iowa/destination.toml
  dev/snowflake/iowa.sql
  prod/el/iowa/main.py          ← copy
  prod/el/iowa/requirements.txt ← copy
  prod/el/iowa/destination.toml ← copy
  prod/snowflake/iowa.sql       ← copy
```

**After (Pillar 1.1):**
```
el/
  iowa/
    main.py
    requirements.txt
    destination.toml          # gains [default] + [<target>] sections (P1.1-02)
    snowflake.sql.j2          # Jinja template (P1.1-03)
```

`targets/` goes away entirely. The `target` column on `runs` and `builds` stays — it records which target a given execution was tested against, not where the files live.

## What stays unchanged

> **Updated during implementation (2026-05-14):** `carve target show` no longer enumerates EL artifacts — it points the user at `carve el list` instead (redundant once artifacts are flat). `carve target delete` and `carve target rename` no longer touch `targets/<name>/` at all; any legacy tree is left in place for the user to clean up manually. The "refuse delete on non-empty target dir" safety rail is therefore gone (its premise is moot under the flat layout). The target subcommand family is now purely connection-config; nothing under `targets/` is touched by any of them.

- `carve target create`, `carve target list`, `carve target show`, `carve target rename`, `carve target delete` — they operate on `connections.toml` sections + `.env.example` blocks. Nothing target-related lives under `targets/` anymore, but the target *abstraction* survives intact. `carve target show` now prints connection config only and refers the user to `carve el list` for artifacts. `carve target delete` / `rename` do not touch any legacy `targets/<name>/` tree; the user is responsible for cleaning up the legacy filesystem if they want to.
- `[snowflake.<target>]` sections in `carve/connections.toml`.
- `<TARGET>_SNOWFLAKE_*` env-var prefix convention.
- `--target X` flag resolution (CLI flag → `CARVE_TARGET` env → `default_target` → `"dev"`).
- `CARVE_ACTIVE_TARGET` (uppercased) injection into subprocesses by the runner.
- `runs.target` and `builds.target` columns; the `latest_build_for(pipeline, target)` repository helper.
- Connection-context preamble in the build agent's system prompt (script-side env-var refs, DDL-side resolved values — though the DDL side becomes "destination resolved AT DEPLOY TIME for the named target," see P1.1-03).

## Implementation

### File-level changes

**Modified:**

- `src/carve/cli/orchestrator/builder.py` — `pipeline_dir_rel` becomes `f"el/{pipeline_name}"` (was `f"targets/{active_target}/el/{pipeline_name}"`). The build flow still resolves an `active_target` (catalog inspection at build time uses the active target's connection), and the resulting Build row still stamps `target=<active_target>` — but the files land in the flat tree. Drop the legacy `pipelines/<name>/` fallback (the deprecation is from M1.1-06; this is when it goes away).
- `src/carve/cli/orchestrator/runner.py` — primary path resolution becomes `el/<name>/`. The legacy `targets/<active>/el/<name>/` path is checked as a one-version fallback with a deprecation warning instructing the user to run `git mv targets/dev/el el` (the migration recipe). `pipelines/<name>/` legacy fallback from M1.1-06 / P1-07 is finally dropped.
- `src/carve/cli/commands/el/list.py` — iterates `el/*/` rather than `targets/<active>/el/*/`. The "last run target" column shows a per-target rollup so the user can see "iowa: dev=success, prod=success, staging=failed" at a glance instead of having to filter by target.
- `src/carve/cli/commands/init.py` — drops the `_ensure_dir(root / "targets")` calls (and any per-target initial subdir creation). Creates `el/` at init time instead (empty; the first build populates it). The `add_target_to_project` helper from P1-01 stays — it operates on `connections.toml` and `.env.example` only, which are unchanged here.
- `src/carve/cli/commands/target/create.py` (etc.) — no longer creates `targets/<name>/el/`. The target subcommand family becomes purely about connection config (which it should always have been).
- `src/carve/cli/orchestrator/listing.py` — pipeline rendering shows `el/<name>` for the file path column.
- `src/carve/core/state/repository.py` — `create_or_update_pipeline` accepts `pipeline_dir` as before but the canonical value is `f"el/{name}"` rather than per-target. No schema change.
- `src/carve/core/agents/extract_load/agent.py` — `_allow_listed_paths` returns `el/<name>/{main.py,requirements.txt}` (no target prefix). Output-paths block in the system prompt reflects the same.
- `src/carve/core/agents/prompts/m1_build_agent.md` — output-path wording: files go under `el/<name>/`, not `targets/<active>/el/<name>/`. The connection-context block's intent is unchanged (env-var refs for script, resolved values for DDL).
- `src/carve/core/agents/recovery/agent.py` — `_allowed_write_paths` updated. Recovery agent writes to `el/<name>/` paths (not per-target).
- `migrations/versions/0004_build_entity.py` — no migration change needed; `builds.target` stays.

**Deleted:**

- All test fixtures that plant artifacts under `targets/<X>/el/<name>/` — retargeted to `el/<name>/`.
- The `targets_dir` config field in `carve.toml`'s `[paths]` section is dropped from new templates (`carve init` no longer emits it). Existing projects' `carve.toml` keeps it (pydantic schema retains the field with a default) but it's a no-op.

> **Known deviation (2026-05-14):** `_CARVE_TOML_TEMPLATE` in `src/carve/cli/commands/init.py:46` still emits `targets_dir = "targets"`. This was missed during implementation; spec intent is unchanged. Tracked as a small follow-up — `carve init` should stop emitting this line. The field stays in `core/config/schema.py` (pydantic schema retains it as a no-op default for existing projects), as the spec describes; only the template emission needs to be dropped.

**Added:**

- One-line deprecation warning in the runner when it falls through to the legacy `targets/<active>/el/<name>/` path. Removed entirely in v0.2.

### State store

No migration. `builds.target` and `runs.target` retain their meaning ("which target's catalog/runtime this build/run is associated with"), but neither column constrains the artifact's location anymore.

`Pipeline.pipeline_dir` stays as `el/<name>` in all new builds. Existing rows with `targets/<X>/el/<name>` keep working through the legacy-fallback shim until v0.2.

## Tests

- `test_build_writes_to_flat_el_path` — `carve build <plan_id>` writes to `el/<name>/main.py`, not `targets/dev/el/<name>/main.py`.
- `test_run_resolves_from_flat_el_path` — `carve el run <name>` reads `el/<name>/main.py`.
- `test_run_legacy_targets_path_fallback_warns` — when `el/<name>/` is absent and `targets/<active>/el/<name>/` exists, run falls back with a deprecation warning instructing migration.
- `test_run_legacy_pipelines_path_removed` — `pipelines/<name>/` (the M1.1-06 legacy) no longer works; users must `git mv` before running.
- `test_list_shows_per_target_rollup` — `carve el list` shows artifacts under `el/` with a column per target observed in the `runs` table.
- `test_target_create_does_not_create_targets_dir` — `carve target create staging` adds the `connections.toml` section and `.env.example` block; does NOT create `targets/staging/`.
- `test_init_no_longer_creates_targets_subtree` — `carve init` writes `el/` (empty) and connections + env-example; does NOT write `targets/`.
- `test_existing_targets_dir_left_alone` — when a user's project already has `targets/`, `carve init` does not touch it (idempotency).
- `test_build_target_column_records_active_target` — `carve build --target staging` still stamps `builds.target = "staging"` even though files land in `el/<name>/`. The target reflects catalog-inspection context, not file location.

## Acceptance criteria

- `carve init` creates `el/` (empty); does NOT create `targets/`.
- `carve build <plan_id>` writes files to `el/<name>/`.
- `carve el run <name> --target X` reads `el/<name>/main.py` and runs it against target X.
- `carve el list` enumerates `el/*/`, showing per-target last-run rollups.
- Legacy `targets/<active>/el/<name>/` is supported as a deprecated fallback with a warning; legacy `pipelines/<name>/` no longer works.
- `runs.target` and `builds.target` continue to record the target each run/build was associated with.
- All M1, M1.1, P1-* tests pass after path-assertion retargeting.
- `ruff` + `mypy --strict` + `pytest` stay green.

## Files this spec produces

(Summary of File-level changes section.)

Modified: `cli/orchestrator/{builder,runner,listing}.py`, `cli/commands/{init,el/list,target/create,target/show,target/delete,target/rename}.py`, `core/agents/extract_load/agent.py`, `core/agents/prompts/m1_build_agent.md`, `core/agents/recovery/agent.py`, existing tests across `tests/cli/`, `tests/core/`.

No DB migrations. No new source modules.

## Out of scope

- `destination.toml` shape changes — that's P1.1-02. This spec leaves destination.toml format as-is; it just moves the file location.
- DDL template changes — that's P1.1-03. The `.sql` file moves to `el/<name>/snowflake.sql` (still a per-target snapshot) in this spec; P1.1-03 templatizes it.
- Deploy command rework — that's P1.1-03/04.
- A `carve migrate` command. Manual recipe in `CHANGELOG.md` is the upgrade path.

## What this enables

- The same `el/<name>/main.py` runs against any target by switching env-var prefixes. No code duplication across targets.
- Git answers "what version is in prod" — via commit SHA, branch, or tag. Carve no longer duplicates that signal with a folder copy.
- `carve target create staging` becomes a 5-second operation: append a `[snowflake.staging]` block + `.env.example` block. No directory tree.
- Future pillars (Pillar 2 dbt models, Pillar 3 pipeline defs, Pillar 4 schedule defs) inherit the flat layout naturally — `dbt/`, `pipelines/`, `schedules/` at the project root, not `targets/<X>/dbt/`, etc.
