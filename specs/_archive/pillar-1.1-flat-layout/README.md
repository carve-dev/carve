# Pillar 1.1 — Flat layout + git-based promotion

**Duration:** ~3 days
**Goal:** Simplify Pillar 1's promotion model to match how data teams actually deploy in 2026: one code tree per artifact, environment-specific config via env vars + per-target sections, promotion via git. Drop per-target file copies and the `--from X --to Y` deploy model.

## Why this exists

Pillar 1 (v0.1.0) shipped with a per-target folder structure:

```
targets/
  dev/el/iowa/{main.py, requirements.txt, destination.toml}
  dev/snowflake/iowa.sql
  prod/el/iowa/{main.py, requirements.txt, destination.toml}
  prod/snowflake/iowa.sql
```

Promotion happened via `carve el deploy iowa --from dev --to prod`, which copied files + applied DDL + verified.

Dogfooding surfaced problems with that model:

1. **Code duplication across targets.** The same `main.py` lives in `targets/dev/el/iowa/` AND `targets/prod/el/iowa/`. Drift inside one git checkout is now possible. Users have to remember "deploy is what syncs them" — a concept they don't have in dbt or anywhere else.

2. **`--from X --to Y` couples two targets' credentials on one machine.** Real prod deploys run from CI with only prod credentials. The model assumed either a developer's laptop with both sets, or CI with both — neither is the norm.

3. **The "file copy" deploy primitive duplicates what git already does.** `git checkout main` is "this is what's in prod." Adding a `copy from dev/ to prod/` step on top makes git history less authoritative.

Pillar 1.1 flips to the model dbt, Alembic, and most data-tooling shops use:
- **One code tree per artifact** at `el/<name>/`.
- **Git** answers "what version is in prod" (branches / tags / commits).
- **`carve el deploy <name> --target X`** is "make target X's destination state ready to receive runs of this artifact" — DDL apply + smoke verify, no file copy. Runs in CI with only X's deploy creds.
- **`carve el run <name> --target X`** runs the script against X with X's runtime creds. Same code, different target.
- **`destination.toml`** carries `[default]` + per-target `[X]` sections in one file. The script picks the right section at runtime.
- **DDL** is a Jinja template (`snowflake.sql.j2`) rendered at deploy time per target. Same template, target-specific identifiers.

## What survives from Pillar 1

The verbs and core mechanisms stay:

- `plan → build → run → deploy` lifecycle.
- `[snowflake.<target>]` sections in `carve/connections.toml`, `<TARGET>_SNOWFLAKE_*` env vars.
- Deploy role vs runtime role split.
- Idempotent DDL contract (CREATE IF NOT EXISTS, GRANT, ALTER ADD COLUMN IF NOT EXISTS; no CREATE OR REPLACE, no bare RENAME).
- DDL allow-list at apply time.
- Recovery agent across the 4 trigger contexts.
- Extract-load specialist, skill registry, catalog skills.
- `destination.toml`'s table-always-literal + database/schema-optional-overrides rule.
- `--target X` resolution (CLI flag → `CARVE_TARGET` env → `default_target` → `"dev"`).
- The `target` column on Runs and Builds.

## What changes

- File layout: `targets/<target>/el/<name>/` → `el/<name>/` (single tree per artifact).
- DDL file: per-target snapshot `.sql` → Jinja template `.sql.j2` rendered at deploy.
- `destination.toml`: one file per target → one file per artifact with `[default]` + `[<target>]` sections.
- `carve el deploy --from X --to Y` → `carve el deploy --target X`. No file copy phase.
- Builder no longer writes to `targets/<active>/`. Build artifacts live at `el/<name>/`.

## Spec list (recommended build order)

| # | Spec | Purpose | Lineage |
|---|---|---|---|
| 01 | [flat-layout](./01-flat-layout.md) | Move artifacts to `el/<name>/`. Builder, runner, list command path updates. Migration recipe for existing projects. | Replaces P1-01's per-target folder semantics |
| 02 | [destination-with-sections](./02-destination-with-sections.md) | `destination.toml` with `[default]` + per-target sections; script reads `[<active_target>]` then `[default]` then env vars. | Supersedes Pillar 1 stages 1+2 of the destination.toml work (commits e4eb505 + 743daab) |
| 03 | [templated-ddl-and-deploy](./03-templated-ddl-and-deploy.md) | `snowflake.sql.j2` Jinja template, rendered at deploy. `carve el deploy <name> --target X` (no `--from`). | Supersedes P1-06 (DDL snapshot) and P1-08 (deploy --from/--to) |
| 04 | [recovery-and-cicd-docs](./04-recovery-and-cicd-docs.md) | Recovery agent path updates + CI/CD docs explaining the post-merge deploy + scheduled run pattern. | Extends P1-09; updates `docs/deploy-from-ci.md` |

Each spec is `/build-spec`-able independently but assumes the prior spec has landed (P1.1-02 reads `destination.toml`; P1.1-03 uses the section-based destination; P1.1-04 references the new deploy/run paths).

## Migration story for users on v0.1.0

A short recipe lands in `CHANGELOG.md` under the v0.1.1 entry:

```bash
# 1. Flatten the artifact tree.
git mv targets/dev/el el
rm -rf targets/      # everything else under targets/ is generated

# 2. Merge per-target destination.toml files into one per artifact.
#    Each `el/<name>/destination.toml` becomes a single file with
#    [default] + [<target>] sections (manual edit; the layouts are
#    simple enough that a one-shot script would over-engineer).

# 3. Rename per-target DDL files to a template alongside their script.
git mv targets/dev/snowflake/<name>.sql el/<name>/snowflake.sql.j2
# Edit the .sql.j2 to replace concrete identifiers with placeholders:
#   ANALYTICS_DEV  →  {{ database }}
#   RAW            →  {{ schema }}
#   IOWA_LIQUOR    →  {{ table }}
#   TRANSFORMER_DEV →  {{ runtime_role }}

# 4. Switch CI/CD workflows from `--from X --to Y` to `--target X`
#    and split deploy creds from runtime creds (see docs/deploy-from-ci.md).
```

We don't ship a `carve migrate` command. The flatten is short enough that the ceremony of an automated migration isn't worth the maintenance — and a human reading the diff catches issues an automated migration would silently land.

## Definition of done

- All 4 specs implemented with tests.
- Existing P1-01..P1-09 tests retargeted to the flat layout pass.
- A 3-minute screen recording of the demo flow: `carve init → fill in .env → carve plan → carve build → carve el run --target dev → carve el deploy --target prod → carve el run --target prod`.
- `docs/deploy-from-ci.md` updated with GitHub Actions snippets for both the post-merge deploy and the scheduled run.
- Internal tag `v0.1.1`.

## What's deliberately deferred to Pillar 2+

- A `carve migrate` command to flatten existing projects. Manual recipe is enough at current adoption.
- Detection-and-warn when reading a legacy `targets/<X>/el/` path (we hard-break; v0.1.0 users are few and explicit guidance in CHANGELOG covers them).
- The `--from X --to Y` deploy as a power-user verb (e.g., "promote dev's committed snapshot to prod"). Out of scope — git already answers this.
- Multi-target deploys in a single command (`--targets dev,staging,prod`). Defer.
- Schema migration files for non-idempotent operations (DROP COLUMN under data-preserve, RENAME with backfill). Defer to Pillar 2.

## Spec authoring conventions (carry-over from P1)

- Each spec carries an explicit **Lineage** field naming the P1-* ancestor it supersedes or extends.
- Each spec has scope, interfaces, file paths, acceptance criteria, tests, estimated effort.
- Specs are self-contained — they list their dependencies on other specs at the top.
