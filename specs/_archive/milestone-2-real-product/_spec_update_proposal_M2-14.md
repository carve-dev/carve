# M2-14 — Deploy orchestration (PR + provisioning + workflow)

**Milestone:** 2 — Real product
**Estimated effort:** 2.5–3 days
**Dependencies:** M1.1-06 (plan/build/run/deploy lifecycle), M2-01 (Build entity, plan/deploy workflow), M2-15 (recovery agent — Phase 1 hook)

## Update notes (proposal)

The original M2-14 was scoped to "open a PR for the built pipeline."
That framing left every other deploy step — DDL, RBAC, migrations,
smoke verification, and the GitHub Actions plumbing that drives them —
in nobody's spec. This rewrite expands M2-14 to own the **full deploy
lifecycle**, with PR creation as one phase of five.

`carve deploy` is now the contract that takes a Build and lands it in a
production Snowflake target with all required side effects executed
under appropriate roles, observable in CI, and recoverable when
pre-flight fails.

What changed vs. the prior proposal:

- The 5-phase deploy (pre-flight, code shipment, provisioning, DML
  migration, verification) replaces the "branch + push + open PR" flow.
- The PR is no longer the deploy. The PR is the artifact of Phase 2;
  the deploy *completes* when the merged commit's GitHub Actions
  workflow finishes Phase 5.
- Carve generates and owns
  `.github/workflows/carve-deploy-<pipeline>-<target>.yml`. Merging the
  PR triggers it; the workflow re-invokes `carve deploy --post-merge`
  to run Phases 3–5 inside CI.
- Targets are now an explicit input to `carve deploy`, with `--target`
  overriding the build's default target. (M2-01's table forbade
  `--target` on `carve deploy`; that table needs an update — see
  "Cross-spec changes" below.)
- A separate Snowflake **deploy role** (with CREATE/elevated privilege)
  is introduced for Phases 3–4. The runtime role used by `carve run`
  stays DML-only.
- Migrations live in `pipelines/<name>/migrations/NNN_slug.sql`. The
  contract is **idempotency, not tracking**: every migration runs on
  every deploy, ordered alphabetically, with no `_carve_migrations`
  table in the user's account.
- AI-assisted recovery is permitted only in Phase 1 (no prod writes
  have happened yet). It hands off to the recovery agent (M2-15).
  Phases 3–5 fail loud as failed CI checks; the user investigates.
- Deploy state per target is queried from `runs` rows
  (`kind="deploy"`, `target_id=<pipeline>`, `target=<target>`); the
  `pipelines` row does not duplicate this state.

Auth, branch naming, commit-message and PR-body templates,
GitHub-only-for-v0.1 reasoning, and the `Provider` abstraction from the
prior proposal carry over; templates are extended to include the
deployment manifest.

## Purpose

`carve deploy <pipeline_name> [--target <name>]` promotes a Build of a
pipeline into a production Snowflake target. Concretely it (1)
validates the target is in a state where the deploy can land, (2)
opens a PR carrying pipeline code, generated DDL, migrations, and a
generated GitHub Actions workflow, and (3) the workflow — triggered
on merge — provisions, migrates, and verifies the target.

The verb spans two execution contexts: the user's local machine
(Phases 1–2) and GitHub Actions (Phases 3–5). Carve generates the
workflow file that bridges them; the same `carve` binary runs in both.

## The 5-phase deploy

`carve deploy <pipeline_name> [--target X] [--abandon-existing]` runs
Phases 1–2 locally, then opens (or updates) a PR. Phases 3–5 run on
merge inside the generated workflow.

### Phase 1 — Pre-flight (local, no prod writes)

- Resolve `Pipeline → current_build_id → Build`. Reject with exit 2 if
  there is no current build.
- Resolve target: `--target` if passed, otherwise `Build.target`. Look
  up the target in `carve/connections.toml` using the **deploy role**
  connection (e.g. `[snowflake.prod_deploy]`).
- Connect with the deploy role; verify connectivity with
  `SELECT CURRENT_ROLE()`.
- Compute the **deployment manifest** (`DeploymentManifest`):
  - Generated DDL files at `snowflake/<pipeline>.sql` (produced by the
    Snowflake specialist agent at build time, M2-05).
  - Migration files under `pipelines/<name>/migrations/*.sql`,
    discovered alphabetically.
  - Pipeline source files under `pipelines/<name>/`.
  - Generated workflow YAML at
    `.github/workflows/carve-deploy-<pipeline>-<target>.yml`.
- **Drift checks** vs. current target state:
  - Destination tables exist with the columns the build expects.
  - Required grants for the runtime role are present.
  - Required GitHub repo secrets exist (e.g.
    `SNOWFLAKE_DEPLOY_USER`, `SNOWFLAKE_DEPLOY_PRIVATE_KEY`). When
    Carve cannot read repo secrets directly (PAT scope), report which
    names the workflow expects and ask the user to verify.
- AI-assisted recovery is allowed — no prod writes have happened. The
  recovery agent (M2-15) takes the failure category, the manifest, and
  the drift report, and emits one of `apply_patch` (rebuild required;
  Carve drives that handoff), `request_replan`, or `give_up` with a
  suggested user action. Phase 1 honors the same `[runner.auto_fix]`
  budget as `carve run`.

### Phase 2 — Code shipment + workflow generation (local, writes to git)

- Branch from `main` using the configured naming template
  (`carve/<pipeline_name>-<short_id>`).
- Stage and commit:
  - `pipelines/<name>/` (full directory).
  - `snowflake/<pipeline>.sql` (DDL files).
  - `pipelines/<name>/migrations/NNN_slug.sql` (any migrations the
    build produced for this revision).
  - `.github/workflows/carve-deploy-<pipeline>-<target>.yml` (rendered
    from `templates/workflow.yml.j2`).
- Push the branch.
- Open a PR via the configured Provider. The PR body includes the
  rendered deployment manifest: which DDL files apply, which
  migrations run, which destination objects are expected post-merge.
- **Existing PR handling.** If a PR is already open for the same
  pipeline+target (resolved by querying `runs` and the GitHub API for
  the configured branch pattern):
  - Default: push new commits to the existing branch (no new PR).
  - With `--abandon-existing`: close the old PR with a "superseded"
    comment, then open a fresh branch + PR.
- Persist a `runs` row with `kind="deploy"`,
  `target_id=<pipeline_name>`, `target=<target>`, recording the PR url
  and head SHA. After commit lands, also write `Build.commit_sha`,
  `Build.pr_url`. `Build.deployed_at` is set on Phase 5 success.

A push or PR-create failure marks the deploy run failed; the user
fixes the underlying issue and re-runs `carve deploy`. The short-id
suffix guarantees a fresh branch on retry.

### Phase 3 — Provisioning (post-merge, in GitHub Actions)

Triggered by `push` to `main` against any of the watched paths
(pipeline files, DDL files, the workflow itself). The workflow runs
`carve deploy --post-merge --pipeline <name> --target <target>`, which
in Phase 3:

- Connects with the deploy role.
- Applies the DDL files in `snowflake/<pipeline>.sql` (CREATE SCHEMA,
  CREATE TABLE, CREATE STAGE, etc.).
- Applies the RBAC grants the manifest specifies (the runtime role
  needs SELECT/INSERT/UPDATE on the destination tables, USAGE on the
  warehouse, etc.).

DDL is expected to be idempotent (`CREATE OR REPLACE` /
`CREATE … IF NOT EXISTS`); the Snowflake agent (M2-05) generates it
that way.

### Phase 4 — DML migrations (post-merge)

- Lists `pipelines/<name>/migrations/*.sql` in alphabetical order.
- Runs **every** migration on **every** deploy.
- **No tracking table** in the user's Snowflake account. Idempotency
  is the contract.
- Migrations are required to be idempotent: `CREATE TABLE IF NOT
  EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, `MERGE`,
  conditional `UPDATE … WHERE`, etc.
- For genuinely non-idempotent operations (DROP COLUMN, RENAME), the
  user wraps them in conditional Snowflake patterns (e.g. checks
  against `INFORMATION_SCHEMA`) or accepts that the migration is
  one-time and removes it from the directory after a successful
  deploy.
- M3 may add richer migration semantics (multi-statement transactions,
  conditional execution, rollback). Out of scope for M2.

### Phase 5 — Verification (post-merge)

- Confirms destination tables exist with the expected columns (the
  manifest's `expected_destination_state` block).
- Confirms grants are in place for the runtime role.
- Runs a smoke check — for now, a benign `SELECT 1 FROM <destination>
  LIMIT 0` per destination plus the manifest-defined grants check. M3
  may extend to a sample pipeline run.
- On success: workflow exits 0; the post-merge step updates the deploy
  `Run` row to `success` and stamps `Build.deployed_at`. State writes
  flow through the same Carve state DB the local CLI uses (in M2 the
  workflow updates a remote-readable record; SaaS-mode write-back is
  M3).
- On failure: workflow exits non-zero. The failed GitHub check appears
  on the merged commit and (with branch policies) on subsequent PRs.
- **No AI recovery in CI for M2.** Failed deploys surface as failed
  Actions runs; the user reads logs, fixes manually or refines + rebuilds,
  and re-runs the workflow or pushes a follow-up commit.

## Roles and target separation

- **Deploy role** (`[snowflake.<env>_deploy]` in
  `carve/connections.toml`): CREATE / ALTER / GRANT privilege on the
  destination schema. Used only by Phases 1, 3, and 4. Never used by
  `carve run`.
- **Runtime role** (`[snowflake.<env>]`): DML-only. Used by `carve run
  --target <env>` after the deploy lands.

Carve documents the recommended SQL to set up both roles
(README/onboarding); this spec ships only the *consumer*.

## The generated workflow file

Carve owns `.github/workflows/carve-deploy-<pipeline>-<target>.yml` and
overwrites it on every deploy. Header comment instructs the user not
to edit it directly:

```yaml
# .github/workflows/carve-deploy-iowa_liquor-prod.yml
# Auto-generated by Carve. Do not edit by hand.
# Edit pipelines/iowa_liquor/schedule.yml and re-deploy. (schedule.yml is M3.)
name: Carve deploy — iowa_liquor → prod
on:
  push:
    branches: [main]
    paths:
      - 'pipelines/iowa_liquor/**'
      - 'snowflake/iowa_liquor.sql'
      - '.github/workflows/carve-deploy-iowa_liquor-prod.yml'
jobs:
  provision:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install carve==${{ env.CARVE_VERSION }}
      - run: carve deploy --post-merge --pipeline iowa_liquor --target prod
        env:
          CARVE_VERSION: 0.0.5
          SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
          SNOWFLAKE_USER: ${{ secrets.SNOWFLAKE_DEPLOY_USER }}
          SNOWFLAKE_PRIVATE_KEY: ${{ secrets.SNOWFLAKE_DEPLOY_PRIVATE_KEY }}
          SNOWFLAKE_WAREHOUSE: ${{ secrets.SNOWFLAKE_WAREHOUSE }}
          SNOWFLAKE_DATABASE: ${{ secrets.SNOWFLAKE_DATABASE }}
          SNOWFLAKE_SCHEMA: ${{ secrets.SNOWFLAKE_SCHEMA }}
```

The workflow runs on push to `main` (not on a schedule —
`schedule.yml` is M3). The Carve binary version is pinned at deploy
time to whatever version generated the workflow, so a future Carve
release that changes deploy semantics doesn't retroactively change CI
behavior.

`carve deploy --post-merge` is a hidden / advanced sub-command:

- Loads the manifest from the just-merged commit.
- Runs Phases 3, 4, 5 in order.
- Exits non-zero on any failure.
- Does **not** invoke the recovery agent (M2 deferral).

## PR review iteration

When a reviewer asks for changes:

1. `carve plan --pipeline <name> "<feedback>"` — plan a refinement.
2. `carve build <new_plan_id>` — produce a new Build (replaces
   `Pipeline.current_build_id`).
3. `carve deploy <pipeline> --target <target>` — detects the open PR
   and pushes new commits to the existing branch.

Reviewers see incremental commits on the same PR. For a clean slate,
pass `--abandon-existing` to close the open PR and start fresh.

## Multi-target deploys (out of scope)

One deploy = one target. Staging-then-prod is two invocations:

```
carve deploy iowa_liquor --target staging
# review, merge, observe
carve deploy iowa_liquor --target prod
```

Each opens its own PR, generates its own workflow file, and tracks
its own deploy run. A "promote staging → prod" verb is M3.

## Configuration

`carve/git.toml` (carries over from prior proposal — auth, PR options,
branch template).

`carve/deploy.toml` (new):

```toml
[deploy]
carve_binary = "carve"            # pinned at deploy time to the current version
workflow_runtime = "ubuntu-latest"
python_version = "3.12"

[deploy.targets.prod]
deploy_connection = "prod_deploy"     # references [snowflake.prod_deploy]
runtime_role = "CARVE_RUNTIME"        # role the workflow grants to

[deploy.targets.staging]
deploy_connection = "staging_deploy"
runtime_role = "CARVE_RUNTIME_STAGING"
```

## Build entity dependency

M2-01 introduces a `builds` table; this spec consumes it:

- **Reads:** `Build.id`, `Build.pipeline_name`, `Build.plan_id`,
  `Build.target`, `Build.manifest_json`.
- **Writes (Phase 2):** `Build.commit_sha`, `Build.pr_url`.
- **Writes (Phase 5):** `Build.deployed_at`.

M2-14 does not modify the `builds` schema; that is M2-01's job.

## Pipeline deploy state per target

Each Pipeline tracks deploy state per target *implicitly* via `runs`
rows. To answer "what's the latest deploy of pipeline X to target Y?":

```sql
SELECT * FROM runs
WHERE kind='deploy' AND target_id=<pipeline_name> AND target=<target>
ORDER BY created_at DESC LIMIT 1
```

The latest deploy run carries the open PR url for the pipeline+target.
Re-querying GitHub for "is there an open PR matching
`carve/<pipeline>-*`?" is the fallback when local state is stale.
This spec adds repository helpers for these queries; it does not add
columns to `pipelines`.

## Authentication

Path 1 — GitHub PAT (v0.1, carries over). `GITHUB_TOKEN` with `repo`
scope (push, open PRs, write workflow files). Workflow secrets are
configured by the user in GitHub Settings; Carve does not write
secrets.

Path 2 — GitHub App: deferred to v0.2.

## Implementation

`src/carve/cli/orchestrator/deployer.py` — Phases 1–2 entry:

```python
def deploy_pipeline(
    pipeline_name: str,
    target_override: str | None,
    abandon_existing: bool,
    config: Config,
) -> DeployOutcome:
    pipeline = repo.get_pipeline(pipeline_name)
    build = repo.get_build(pipeline.current_build_id)
    target = target_override or build.target

    manifest = preflight.run(pipeline, build, target, config)  # Phase 1
    pr_url = code_shipment.run(                                 # Phase 2
        pipeline, build, target, manifest, abandon_existing, config,
    )
    repo.update_build(build.id, commit_sha=manifest.commit_sha, pr_url=pr_url)
    return DeployOutcome.pr_opened(pr_url)
```

`src/carve/cli/orchestrator/post_merge.py` — Phases 3–5 entry:

```python
def deploy_post_merge(
    pipeline_name: str, target: str, config: Config,
) -> int:
    manifest = manifest_module.load_from_repo(pipeline_name, target)
    provision.run(manifest, config)        # Phase 3
    migrate.run(manifest, config)          # Phase 4
    verify.run(manifest, config)           # Phase 5
    return 0
```

The `Provider` abstraction (carries over) keeps GitHub specifics
isolated:

```python
class Provider(Protocol):
    def open_pr(self, request: PullRequest) -> str: ...
    def update_pr(self, pr_url: str, request: PullRequest) -> None: ...
    def close_pr(self, pr_url: str, comment: str) -> None: ...
    def find_open_pr(self, pipeline_name: str, target: str) -> str | None: ...
```

`GitHubProvider` implements it via `PyGithub`; GitLab/Bitbucket slot in
later behind the same interface.

## Templates

- `templates/workflow.yml.j2` — workflow file.
- `templates/pr_body.md.j2` — extended to render the deployment manifest.
- `templates/commit_message.j2` — extended with DDL + migration files.

## Error handling

- **Pipeline / build missing**: exit 2 with actionable message.
- **Deploy connection unreachable**: Phase 1 fails; recovery agent may
  diagnose; ultimately surfaces "configure `[snowflake.<env>_deploy]`".
- **Drift detected**: Phase 1 fails; recovery agent emits feedback
  ("destination has unexpected column X; either drop it manually or
  add it to the build").
- **PR push fails**: Phase 2 fails locally; user retries.
- **Workflow fails (Phase 3/4/5)**: failed CI check on the merged
  commit. User reads Actions logs, fixes manually or re-deploys. No
  auto-recovery.
- **Existing PR with conflicting changes**: default behavior pushes
  new commits; conflicts surface as merge-conflict CI checks the user
  resolves.

## Tests

- `tests/cli/orchestrator/test_deployer.py` — happy path through
  Phases 1–2; each phase's failure mode; existing-PR-update flow;
  `--abandon-existing` flow.
- `tests/cli/orchestrator/test_post_merge.py` — Phases 3–5 happy path
  with mocked Snowflake; provision / migration / verification failures
  each exit non-zero.
- `tests/core/deploy/test_preflight.py` — drift detection (missing
  destination column; extra destination column; missing grant; missing
  required secret).
- `tests/core/deploy/test_manifest.py` — manifest assembly from a
  Build fixture; alphabetical migration ordering; deterministic
  rendering.
- `tests/core/deploy/test_workflow.py` — workflow YAML for: a single
  target; pipeline with migrations; pipeline with no migrations;
  varying Carve binary versions.
- `tests/core/deploy/test_provision.py` — DDL idempotency; grant
  application.
- `tests/core/deploy/test_migrate.py` — alphabetical ordering;
  every-migration-runs-every-time semantics; failure surfaces clearly.
- `tests/core/deploy/test_verify.py` — destination-table check; grant
  check; smoke check.
- `tests/core/git/test_github_provider.py` — open / update / close /
  find-open PR (mocked PyGithub).
- `tests/core/git/test_branch.py` — short-id generation; collision
  retry.
- Phase 1 recovery integration: mocked recovery agent emits
  `apply_patch` and `request_replan`; Phase 1 honors them within budget.

## Acceptance criteria

- `carve deploy <pipeline_name> [--target X]` runs Phases 1–2 end to
  end: pre-flight passes, the PR is opened (or existing PR updated),
  the deployment manifest renders in the PR body, and a `runs` row of
  `kind="deploy"` records the PR url and head SHA.
- The generated workflow file at
  `.github/workflows/carve-deploy-<pipeline>-<target>.yml` is included
  in the PR commit, watches the right paths, and pins the Carve binary
  version.
- Merging the PR triggers the workflow; `carve deploy --post-merge`
  inside CI runs Phases 3, 4, 5 in order and exits 0 when all three
  succeed.
- Phase 4 runs every migration in `pipelines/<name>/migrations/` on
  every deploy, alphabetical, with no tracking table written to the
  user's Snowflake account.
- A failed Phase 1 hands off to the recovery agent (M2-15) within
  `[runner.auto_fix]` budget; non-recoverable failures surface a clear
  diagnosis.
- A subsequent `carve deploy` for the same pipeline+target with an
  open PR pushes new commits to the existing branch by default;
  `--abandon-existing` closes the old PR and opens a new one.
- `carve deploy --target` with a target the build wasn't generated
  against runs Phase 1 drift checks against the override target.
- Phase 5 success on the merged commit stamps `Build.deployed_at` and
  marks the deploy `Run` row `success`; Phase 5 failure leaves the run
  `failed` and surfaces a failed GitHub check.
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover
  every phase's happy path and at least one failure mode each.

## Files this spec produces

New:

- `src/carve/core/git/__init__.py`
- `src/carve/core/git/provider.py` — `GitConfig`, `Provider` protocol,
  `PullRequest` model.
- `src/carve/core/git/github.py` — `GitHubProvider`.
- `src/carve/core/git/branch.py` — branch-name generation, short-id
  helper, collision retry.
- `src/carve/core/deploy/__init__.py`
- `src/carve/core/deploy/manifest.py` — `DeploymentManifest` model;
  manifest assembly from a Build's outputs.
- `src/carve/core/deploy/preflight.py` — Phase 1.
- `src/carve/core/deploy/code_shipment.py` — Phase 2.
- `src/carve/core/deploy/provision.py` — Phase 3.
- `src/carve/core/deploy/migrate.py` — Phase 4.
- `src/carve/core/deploy/verify.py` — Phase 5.
- `src/carve/core/deploy/workflow.py` — workflow YAML generation.
- `src/carve/core/deploy/templates/workflow.yml.j2`
- `src/carve/core/deploy/templates/pr_body.md.j2`
- `src/carve/core/deploy/templates/commit_message.j2`
- `src/carve/cli/orchestrator/deployer.py` — Phases 1–2 orchestrator.
- `src/carve/cli/orchestrator/post_merge.py` — Phases 3–5 orchestrator.
- `tests/core/git/test_github_provider.py`
- `tests/core/git/test_branch.py`
- `tests/core/deploy/test_manifest.py`
- `tests/core/deploy/test_preflight.py`
- `tests/core/deploy/test_workflow.py`
- `tests/core/deploy/test_provision.py`
- `tests/core/deploy/test_migrate.py`
- `tests/core/deploy/test_verify.py`
- `tests/cli/orchestrator/test_deployer.py`
- `tests/cli/orchestrator/test_post_merge.py`

Modified:

- `src/carve/cli/commands/deploy.py` — replace M1.1-06 stub with the
  real implementation; add `--target`, `--abandon-existing`, and the
  hidden `--post-merge` flag.
- `src/carve/cli/main.py` — wire the new flags.
- `src/carve/core/state/repository.py` — `get_build`, `update_build`
  (commit_sha / pr_url / deployed_at); deploy-run helpers; per-target
  latest-deploy lookup.
- `src/carve/core/state/models.py` — extend `Run` with `target` and
  PR url / head sha (engineer's call: separate columns vs. JSON).
- `src/carve/core/config/schema.py` — `DeployConfig` with per-target
  block; `GitConfig` (already partially modeled in the prior proposal).
- `pyproject.toml` — add `PyGithub` runtime dependency.
- `README.md` — full deploy walkthrough including the workflow file.
- `CHANGELOG.md` — entry under `## [Unreleased]`.

## Cross-spec changes flagged (do not make in this spec)

- **M2-01** states `carve deploy` rejects `--target`. This spec needs
  `--target` for staging-then-prod separation. The table in M2-01
  §"`--target` semantics across the lifecycle" needs an update. The
  `carve build` rejection logic for `--target` may also need
  revisiting (the build's target is the *default* the deploy uses,
  but deploy may override).
- **M2-01** introduces the `builds` table; this spec depends on
  `Build.commit_sha`, `Build.pr_url`, and `Build.deployed_at`. Confirm
  these are part of M2-01's schema; if not, M2-01 needs extending.
- **M2-15** documents recovery is not used in `carve deploy`. This
  spec narrows that: recovery *is* used in Phase 1 (no prod writes);
  Phases 3–5 (CI) still don't use it. The "Out of scope: Production
  recovery" bullet in M2-15 should be refined to "Phases 3–5 only."
- **M2-05** (Snowflake agent) must produce
  `snowflake/<pipeline>.sql` and any required migrations at
  `pipelines/<name>/migrations/NNN_slug.sql`. Confirm in M2-05; flag
  if not.
- **ARCHITECTURE.md §7.1** says the prod scheduler is whatever the
  user already operates. After M2-14, the *deploy* generates the
  workflow file that runs the post-merge phases on every push to main.
  Worth a clarifying note (the cron-style scheduler is still M3).

## What this enables

- `carve deploy` is the real boundary between dev and prod, not a
  PR-creation utility.
- Team review of generated DDL + migrations + workflow is the
  human-in-the-loop moment that makes auto-generated infra defensible
  in prod.
- Phases 3–5 in CI mean prod state changes are observable (Actions
  logs), retryable (re-run the workflow), and gated by branch
  protection.
- Deploy idempotency: re-running `carve deploy` against an open PR
  pushes new commits without churn; re-running the workflow re-applies
  idempotent DDL + migrations safely.
- Future GitLab/Bitbucket support is a `Provider` subclass; M3
  scheduling layers on top of the same workflow-generation pipeline.
