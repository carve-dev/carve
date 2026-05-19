# P1.1-04 — Recovery agent updates + CI/CD documentation

**Milestone:** Pillar 1.1 — Flat layout + git-based promotion
**Estimated effort:** 0.5 day
**Dependencies:** P1.1-01 (flat layout), P1.1-02 (sectioned destination.toml), P1.1-03 (templated DDL + simplified deploy)
**Lineage:** Extends **P1-09** (recovery agent across 4 trigger contexts). The recovery agent's tool surface and Invocation dataclasses get path-updated for the flat layout and the template; the CI/CD documentation that ships in `docs/deploy-from-ci.md` gets a full rewrite around the new model.

## Purpose

Two related pieces:

1. **Update the recovery agent's path awareness** so its `write_file` allow-list, `Invocation` dataclass field naming, and `read_run_logs` chain-walking all reflect the flat layout + the new deploy model. The recovery agent's contexts (PREFLIGHT, DDL_APPLY, VERIFY, EL_RUN_FAILURE) stay; their write-allow-lists target `el/<name>/` paths, not `targets/<target>/el/<name>/`.

2. **Rewrite `docs/deploy-from-ci.md`** as the canonical guide to wiring Carve into CI/CD. The current doc (shipped with P1-08) explains `--from X --to Y` and per-target folders; both are gone. The new doc shows the post-merge deploy + scheduled run pattern, with explicit GitHub Actions + GitLab CI snippets and a "where do credentials live" section.

## Recovery agent updates

### Invocation dataclasses

The four `Invocation` dataclasses in `src/carve/core/agents/recovery/invocation.py` lose their `source_target` field (since the new deploy model is single-target):

**Before (P1-09):**
```python
@dataclass(frozen=True, slots=True)
class DeployPreflightInvocation:
    pipeline_name: str
    source_target: str   # the --from
    dest_target: str     # the --to
    project_dir: Path
    config: Config
    failed_run_id: str
    error_text: str
    ddl_path: Path
    drift: tuple[str, ...]
    trigger: ClassVar[TriggerContext] = TriggerContext.DEPLOY_PREFLIGHT
```

**After (P1.1-04):**
```python
@dataclass(frozen=True, slots=True)
class DeployPreflightInvocation:
    pipeline_name: str
    target: str          # the --target
    project_dir: Path
    config: Config
    failed_run_id: str
    error_text: str
    ddl_template_path: Path   # was ddl_path; now points at the .sql.j2 file
    rendered_ddl: str          # NEW: the pre-rendered SQL the apply was about to run
    drift: tuple[str, ...]
    trigger: ClassVar[TriggerContext] = TriggerContext.DEPLOY_PREFLIGHT
```

`rendered_ddl` is new. The recovery agent needs to see the rendered SQL (not just the template) to diagnose a DDL apply failure — the template's `{{ database }}` placeholder is meaningless without the resolved value. The pre-flight / DDL-apply / verify contexts all carry the rendered SQL.

The agent can EDIT the template (`ddl_template_path`); the build flow re-renders on the next attempt. The recovery agent does NOT edit `rendered_ddl` directly — that field is read-only context.

### `write_file` tool allow-list

P1-09's `_allowed_write_paths` returns paths under `targets/<active>/el/<name>/`. Updated to `el/<name>/` paths:

**EL_RUN_FAILURE context (runtime role):**
- `el/<name>/main.py`
- `el/<name>/requirements.txt`
- `el/<name>/destination.toml`   (NEW — recovery may need to override database/schema for the target that's failing)

**DEPLOY_PREFLIGHT context (deploy role):** read-only — no writes.

**DEPLOY_DDL_APPLY / DEPLOY_VERIFY contexts (deploy role for DDL, runtime role for verify):**
- `el/<name>/main.py`
- `el/<name>/requirements.txt`
- `el/<name>/destination.toml`
- `el/<name>/snowflake.sql.j2`   (the template; recovery edits this, deploy re-renders on retry)

Notably absent: the rendered SQL. The template is the source of truth; rendering happens at apply time. Recovery agent never writes rendered SQL to disk.

### `read_run_logs` chain walking

P1-09 pinned `read_run_logs` to `invocation.failed_run_id`. With the unified `--target X` deploy verb, the chain is simpler — no more "phase 2 of the deploy against prod's source-target dev." The walk via `parent_run_id` still works exactly as in P1-09.

### Recovery agent prompt updates

`src/carve/core/agents/prompts/recovery_agent.md` updates:

- "Available actions" table updates to reflect new paths.
- Hard Rule #6 (the dangerous-DDL family forbid-list) is unchanged.
- The trigger-context preamble for DEPLOY_PREFLIGHT / DEPLOY_DDL_APPLY / DEPLOY_VERIFY contexts mentions the template + rendered DDL distinction explicitly: "you may edit `snowflake.sql.j2` (the template); the rendered SQL below is what was about to run / what failed and you cannot edit it directly. Your edit to the template will be re-rendered on retry."
- Drop any "source target" / "dest target" / `--from X --to Y` wording; the deploy is single-target now.

### Implementation

**Modified:**

- `src/carve/core/agents/recovery/invocation.py` — three deploy invocation dataclasses lose `source_target`, gain `rendered_ddl`. Field renames: `ddl_path` → `ddl_template_path`.
- `src/carve/core/agents/recovery/agent.py` — `_allowed_write_paths` updated; tool builders unchanged in shape but the allow-list inputs come from the new paths.
- `src/carve/core/agents/prompts/recovery_agent.md` — preamble wording updated for the new contexts.
- `src/carve/cli/commands/el/deploy.py` — the per-phase `_maybe_recover` builds the new `Invocation` shapes (carrying `rendered_ddl` from the deploy's render step). The deploy flow has the rendered SQL in memory at apply time; passing it through is mechanical.
- `src/carve/cli/orchestrator/recovery.py` — no shape changes; just consumes the updated Invocation dataclasses.
- `src/carve/cli/orchestrator/runner.py` — `ElRunInvocation` doesn't have target-pair fields, so no change beyond the path allow-list passed to `write_file`.
- `src/carve/core/deploy/recovery.py` — `RecoveryContext` Protocol fields stay; `source_target` is dropped from the typed shape (was unused in practice anyway since deploy is single-target).

**Tests:**

- `test_invocation_no_source_target_field` — the dataclasses no longer carry `source_target`; mypy or attribute access fails if anyone references it.
- `test_recovery_writes_to_flat_el_paths` — recovery agent's `write_file` accepts `el/<name>/main.py`; rejects `targets/dev/el/<name>/main.py`.
- `test_recovery_can_edit_snowflake_sql_j2_template` — DDL_APPLY context's allow-list includes the template; the rendered SQL is read-only context.
- `test_recovery_chain_persists_target_not_source_target` — child Run rows carry `target=<X>`, no source_target column to worry about.
- All existing P1-09 recovery tests retargeted to the new Invocation shape.

## CI/CD documentation

`docs/deploy-from-ci.md` gets a full rewrite. Outline:

1. **The model**: git versions code; CI deploys + runs against environments.
2. **Credential split**: deploy role for DDL, runtime role for DML. Separate secrets per env.
3. **GitHub Actions: post-merge deploy.** Sample YAML that runs `carve el deploy <name> --target prod --yes` after a merge to main.
4. **GitHub Actions: scheduled run.** Sample YAML that runs `carve el run <name> --target prod` on a schedule.
5. **GitLab CI**: equivalent snippets.
6. **Other systems** (Airflow, Dagster, custom): the pattern is the same — `carve el deploy --target X` once after merge, `carve el run --target X` per schedule. Pseudocode rather than per-system YAML.
7. **Pre-flight in PR review**: how to wire `carve el deploy --target prod --dry-run` into PR CI so reviewers see the rendered DDL before merging.
8. **Where credentials live**: GitHub repo secrets / GitLab CI variables / Vault / etc. Carve doesn't manage secrets; CI does.

### Sample GitHub Actions content

```yaml
# .github/workflows/deploy-prod.yml — run on merges to main
name: deploy-prod
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install carve
      - name: Deploy iowa_liquor to prod
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          # Deploy-role credentials — CREATE / GRANT / ALTER privileges
          PROD_DEPLOY_SNOWFLAKE_ACCOUNT: ${{ secrets.PROD_DEPLOY_ACCOUNT }}
          PROD_DEPLOY_SNOWFLAKE_USER: ${{ secrets.PROD_DEPLOY_USER }}
          PROD_DEPLOY_SNOWFLAKE_PASSWORD: ${{ secrets.PROD_DEPLOY_PASSWORD }}
          PROD_DEPLOY_SNOWFLAKE_ROLE: ${{ secrets.PROD_DEPLOY_ROLE }}
          PROD_DEPLOY_SNOWFLAKE_WAREHOUSE: ${{ secrets.PROD_DEPLOY_WAREHOUSE }}
          PROD_DEPLOY_SNOWFLAKE_DATABASE: ${{ secrets.PROD_DEPLOY_DATABASE }}
          # Runtime-role credentials — needed only for the post-DDL smoke test
          PROD_SNOWFLAKE_ACCOUNT: ${{ secrets.PROD_ACCOUNT }}
          PROD_SNOWFLAKE_USER: ${{ secrets.PROD_USER }}
          PROD_SNOWFLAKE_PASSWORD: ${{ secrets.PROD_PASSWORD }}
          PROD_SNOWFLAKE_ROLE: ${{ secrets.PROD_ROLE }}
          PROD_SNOWFLAKE_WAREHOUSE: ${{ secrets.PROD_WAREHOUSE }}
          PROD_SNOWFLAKE_DATABASE: ${{ secrets.PROD_DATABASE }}
        run: carve el deploy iowa_liquor --target prod --yes
```

```yaml
# .github/workflows/run-prod.yml — daily scheduled run
name: run-prod
on:
  schedule:
    - cron: "0 6 * * *"  # 6am UTC

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install carve
      - name: Run iowa_liquor against prod
        env:
          # NO deploy creds needed here — runtime role only.
          PROD_SNOWFLAKE_ACCOUNT: ${{ secrets.PROD_ACCOUNT }}
          PROD_SNOWFLAKE_USER: ${{ secrets.PROD_USER }}
          PROD_SNOWFLAKE_PASSWORD: ${{ secrets.PROD_PASSWORD }}
          PROD_SNOWFLAKE_ROLE: ${{ secrets.PROD_ROLE }}
          PROD_SNOWFLAKE_WAREHOUSE: ${{ secrets.PROD_WAREHOUSE }}
          PROD_SNOWFLAKE_DATABASE: ${{ secrets.PROD_DATABASE }}
        run: carve el run iowa_liquor --target prod
```

```yaml
# .github/workflows/pr-preview.yml — PR review with DDL preview
name: pr-preview
on:
  pull_request:
    paths:
      - "el/**"
      - "carve/**"

jobs:
  preview:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install carve
      - name: Render prod DDL (dry-run)
        env:
          # Read-only: dry-run never connects to apply.
          # Deploy creds optional — only needed if you want pre-flight drift in the preview.
          # The rendered DDL is independent of any connection.
          PROD_SNOWFLAKE_DATABASE: ${{ secrets.PROD_DATABASE }}
          PROD_SNOWFLAKE_ROLE: ${{ secrets.PROD_ROLE }}
        run: |
          for artifact in el/*/; do
            name=$(basename "$artifact")
            echo "## ${name}"
            carve el deploy "$name" --target prod --dry-run
          done
```

### Other rewrites

- `docs/deploy-roles.md` — minor updates to drop `--from X --to Y` references. The deploy-role / runtime-role split is unchanged; only the deploy command's invocation shape needs updating.
- `CHANGELOG.md` — new entry for v0.1.1 with the migration recipe (from `pillar-1.1-flat-layout/README.md`'s "Migration story" section, distilled into chronological bullet form).

## Tests

(Recovery-agent path tests covered above; CI/CD docs tested by inclusion in `docs/`. No `--dry-run` test in CI YAML — the GHA snippets are documentation, not tested artifacts.)

- `test_docs_deploy_from_ci_no_from_to`: greps `docs/deploy-from-ci.md` to ensure no `--from` / `--to` references remain.
- `test_docs_deploy_from_ci_mentions_dry_run`: same file references `--dry-run` for the PR-preview pattern.
- `test_changelog_v0_1_1_entry_present`: CHANGELOG has the v0.1.1 entry naming the breaking change.

## Acceptance criteria

- Recovery agent's `Invocation` dataclasses drop `source_target`, gain `rendered_ddl`, rename `ddl_path` → `ddl_template_path`.
- `write_file` allow-list updated to `el/<name>/` paths including `snowflake.sql.j2` and `destination.toml`.
- Recovery agent prompt updated for the new context shape.
- `docs/deploy-from-ci.md` rewritten for the post-merge deploy + scheduled run pattern.
- `docs/deploy-roles.md` updated to drop `--from / --to` references.
- `CHANGELOG.md` carries the v0.1.1 entry with the migration recipe.
- All P1-09 recovery tests retargeted to the new Invocation shape pass.
- `ruff` + `mypy --strict` + `pytest` stay green.

## Files this spec produces

Modified:
- `src/carve/core/agents/recovery/invocation.py`
- `src/carve/core/agents/recovery/agent.py`
- `src/carve/core/agents/prompts/recovery_agent.md`
- `src/carve/cli/commands/el/deploy.py` (Invocation construction at each phase)
- `src/carve/core/deploy/recovery.py` (Protocol field updates)
- `docs/deploy-from-ci.md`
- `docs/deploy-roles.md`
- `CHANGELOG.md`
- existing P1-09 recovery tests retargeted

No new source modules; no DB migrations.

## Out of scope

- A "carve init --ci-template github-actions" command that scaffolds the workflow files. Defer; the docs are clear enough.
- Provider-specific (Snowflake-only) tags on Run rows for CI runs. Defer.
- A `carve el deploy --target X` mode that emits a markdown summary suitable for posting to a PR (PR-preview pattern). The `--dry-run` flag does most of this already; a structured-output flag (`--format json` / `--format pr-comment`) can come later if real users want it.
- Multi-environment promotion telemetry ("did dev deploy 3 days ago? did staging? what's the gap to prod?"). Pillar 4 territory.

## What this enables

- Real CI/CD workflows that mirror dbt's / Alembic's deployment patterns. Users coming from those tools recognize the shape immediately.
- Clear privilege boundaries: the deploy CI job never sees runtime creds (beyond the smoke-test); the scheduled run CI job never sees deploy creds.
- PR review for DDL changes: a reviewer sees the rendered prod DDL in the PR's CI output before merging.
- The recovery agent's contexts stay coherent across the layout pivot — same four triggers, same hard rules, just updated paths.
