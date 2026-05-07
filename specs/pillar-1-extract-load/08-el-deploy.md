# P1-08 — `carve el deploy` (lean, OSS-flexible)

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1 day
**Dependencies:** P1-01 (target system), P1-02 (plan/build lifecycle), P1-06 (Snowflake DDL for EL), P1-07 (`carve el run`)
**Lineage:** Replaces the **parked M2-14 proposal** ([`specs/_archive/milestone-2-real-product/_spec_update_proposal_M2-14.md`](../_archive/milestone-2-real-product/_spec_update_proposal_M2-14.md), drafted but never accepted). The 5-phase ceremony, generated GHA workflow files, and `Provider` abstraction in that proposal are all dropped — too prescriptive for OSS, fights users on Airflow/GitLab/custom CI. Deploy here is a single deterministic command users wrap in whatever workflow they already operate. The deploy-role / runtime-role separation pattern from the parked proposal carries forward as a documented recommendation.

## Purpose

Promote an EL artifact from one target to another by **copying the artifact files into the destination target's folder AND applying the DDL changes in the destination's Snowflake account**. Deploy is one deterministic CLI command. Carve doesn't dictate how the user wraps it (manual, GitHub Actions, GitLab CI, Airflow DAG, custom script — all work).

The user's PR / review / CI/CD layer sits *around* `carve el deploy`, not *inside* it. Carve provides the deterministic action; the user provides the governance.

## CLI surface

```
carve el deploy <name> --from <source_target> --to <dest_target> [--yes] [--no-smoke-test]
carve el verify <name> --target <target> [--no-smoke-test]
```

- `<name>` — required positional. The EL artifact to promote / verify.
- `--from <source_target>` — where to read the artifact files from (deploy only).
- `--to <dest_target>` — where to land the artifact (deploy only).
- `--yes` — skip the confirmation prompt before any writes.
- `--target <target>` — target to verify against (verify only).
- `--no-smoke-test` — disable the post-apply `SELECT 1 FROM <destination> LIMIT 1` check. Default: smoke test runs (it's a single cheap query per destination; opt out only if connectivity costs are a concern).

`--from` and `--to` must be different and both defined in `carve/connections.toml`. There is no single-target deploy form.

## The deploy flow

`carve el deploy <name> --from <X> --to <Y>` runs end-to-end:

1. **Validate.** Both targets defined in `carve/connections.toml`. Source artifact files exist under `targets/<X>/el/<name>/`. The artifact has a successful Build row in the DB for `<X>`.
2. **Pre-flight (read-only against `<Y>`).** Connect with `<Y>`'s deploy role. Verify connectivity. Drift-check the destination's existing schema against the build's manifest:
   - Does the destination schema exist or will the DDL create it?
   - If the destination table exists, do its columns match what the build expects?
   - Does the runtime role exist?
3. **Recovery on drift.** If drift is recoverable (missing column, missing schema, etc.), hand off to the recovery agent (P1-09) within the configured budget for an auto-fix attempt at the source — typically a refined plan that produces a build matching the destination's actual state, or an explicit decision to apply additive DDL. Phase 2 has not happened yet, so AI experimentation is safe.
4. **Confirmation prompt** (skipped with `--yes`). Print the deploy plan: which files will be copied, which DDL statements will be applied, against which target. User confirms.
5. **Copy files.** Mirror `targets/<X>/el/<name>/` → `targets/<Y>/el/<name>/`. Mirror `targets/<X>/snowflake/<name>.sql` → `targets/<Y>/snowflake/<name>.sql`. Working-tree changes; the user commits afterward (or wraps the command in CI that commits for them).
6. **Apply DDL.** Connect with `<Y>`'s deploy role. Read `targets/<Y>/snowflake/<name>.sql` (just-copied). Execute statements in order. On any failure: hand off to the recovery agent (P1-09) within budget for an auto-fix attempt — examples below. On recovery exhaust: stop, exit non-zero with the failing statement, Snowflake's error, and the recovery agent's diagnosis.
7. **Smoke verify.** Run the `verify` flow's checks (destination tables exist with expected columns; runtime role grants in place; `SELECT 1` reachable per `--no-smoke-test`). On verification failure: hand off to the recovery agent for an auto-fix attempt within budget; on exhaust, exit non-zero. Idempotent DDL means the deploy can be safely re-run after manual intervention.
8. **Record.** Persist a `Run` row: `kind="deploy"`, `pipeline_name=<name>`, `target=<Y>`, `target_id=<build_id>`, `status` set per outcome.
9. **Success message.** Print a summary: files copied, DDL statements applied, target's runtime role now able to execute the script.

The whole flow is one command. No PR machinery, no branch management, no provider integration. Carve writes to the working tree; the user's git/CI/CD workflow takes over from there.

### What `carve el deploy` does NOT do

- Does **not** open a PR. If the user wants PR review, they wrap deploy in a workflow that runs deploy on the PR branch, then merges after review. Or runs deploy post-merge from CI. Or doesn't use PRs at all. Their call.
- Does **not** commit the file copies. After deploy succeeds, `git status` shows the modified files; the user commits.
- Does **not** push to a remote. Same reasoning — user owns git.
- Does **not** manage GitHub-, GitLab-, or any-other-provider state. There's no `Provider` abstraction; no need for it.
- Does **not** know about CI environments. A user invoking `carve el deploy` from a GHA runner gets the same behavior as one running it from their laptop. Deterministic, scriptable, simple.

## The verify flow

`carve el verify <name> --target <target>` is a read-only sanity check, separately useful:

1. Resolve `<target>`. Connect with the **runtime role** (`[snowflake.<target>]`).
2. Read the artifact's most recent successful Build's manifest_json (P1-02) — which destinations the build expects.
3. For each destination table: confirm it exists with the expected columns. Surface column-by-column drift if any.
4. Confirm the runtime role has `SELECT, INSERT, UPDATE, DELETE` privileges on each destination.
5. Run a `SELECT 1 FROM <destination> LIMIT 1` against each destination to confirm queryability. Always-on by default; opt out with `--no-smoke-test`. Cheap (one row, one warehouse-second per destination) but adds round-trip latency on slow networks — opt out only if you need to.

Returns 0 on all checks pass; non-zero with diagnosis on any failure. Verify is also called internally by `carve el deploy` (step 7), but exposing it as a standalone command is useful for:

- Ad-hoc dev sanity checks ("is my dev target still in the state I expect?")
- CI gates separate from deploy ("verify before merging the PR; deploy after")
- Operational debugging after a manual Snowflake change ("did I break anything?")

## Deploy role / runtime role pattern

Pillar 1 documents (in the README and this spec) a recommended two-role pattern per target:

- **Runtime role** — used by `carve el run`. DML-only on the destination tables. Configured at `[snowflake.<target>]` in `connections.toml`.
- **Deploy role** — used by `carve el deploy` (specifically the DDL-application step). CREATE/ALTER/GRANT privileges on the destination schema. Configured at `[snowflake.<target>_deploy]`.

Sample `carve/connections.toml`:

```toml
[snowflake.prod]
account   = "${PROD_SNOWFLAKE_ACCOUNT}"
user      = "${PROD_SNOWFLAKE_USER}"
password  = "${PROD_SNOWFLAKE_PASSWORD}"
role      = "TRANSFORMER_PROD"          # runtime role: DML only
warehouse = "${PROD_SNOWFLAKE_WAREHOUSE}"
database  = "${PROD_SNOWFLAKE_DATABASE}"

[snowflake.prod_deploy]
account   = "${PROD_SNOWFLAKE_ACCOUNT}"
user      = "${PROD_SNOWFLAKE_DEPLOY_USER}"
password  = "${PROD_SNOWFLAKE_DEPLOY_PASSWORD}"
role      = "DEPLOYER_PROD"             # has CREATE on PROD.RAW + GRANT to TRANSFORMER_PROD
warehouse = "${PROD_SNOWFLAKE_WAREHOUSE}"
database  = "${PROD_SNOWFLAKE_DATABASE}"
```

The pattern is a **recommendation, not enforced by code**. `carve el deploy` simply requires the deploy connection to exist (`<target>_deploy` is the convention) and to have the privileges the DDL needs. The `_deploy` suffix is convention, not a magic string.

For dev (single-developer), users can point both `[snowflake.dev]` and `[snowflake.dev_deploy]` at the same role with full privileges. The separation only meaningfully matters for prod-class targets where audit trails and least-privilege hygiene matter.

If the deploy connection is missing, deploy fails Phase 1 with: `"No '<target>_deploy' connection in carve/connections.toml. See docs/deploy-roles.md for the recommended deploy/runtime role pattern."`

## Calling deploy from CI/CD

The user's CI/CD invokes `carve el deploy` like any other CLI command. We provide one **example doc snippet** at `docs/deploy-from-ci.md` showing the basic shape:

```bash
# In your CI pipeline (GitHub Actions, GitLab CI, Airflow PythonOperator, etc.):
pip install carve
carve el deploy iowa_liquor_sales --from dev --to prod --yes
```

Plus a paragraph or two explaining:
- Set the `<TARGET>_SNOWFLAKE_*` env vars (including the `_DEPLOY_*` variants) as CI secrets.
- Run on whatever event makes sense for your team (PR merge, manual approval, scheduled, etc.).
- Idempotent: re-running deploy on an unchanged source is safe (DDL is idempotent per P1-06; file copy is a no-op if files match).
- Track deploy history via `carve runs --pipeline <name>` (filtered to `kind="deploy"`).

We do **not** ship a per-pipeline-per-target generated workflow file. We do **not** ship vendor-specific GHA / GitLab / Airflow templates beyond the generic snippet. Users adapt the one-liner to their stack.

## Recovery agent integration

The recovery agent (P1-09) participates in **three distinct deploy contexts**, each within the configured fix-attempt budget (default: 3 attempts, $1.00 cap, per `carve/runner.toml` from M1.1-04):

### 1. Pre-flight drift (Phase 1, no prod writes yet)

Examples:
- **Destination column missing.** The build expects column `INVOICE_LINE_NO`; the existing destination doesn't have it. Recovery agent option: emit `ALTER TABLE … ADD COLUMN IF NOT EXISTS INVOICE_LINE_NO …` into the DDL file, or refine the plan to skip that column.
- **Destination column type mismatch.** Build expects `VARCHAR(50)`; existing destination has `VARCHAR(20)`. Recovery agent surfaces options: rebuild against the destination's actual type, or surface "non-idempotent type change required — please apply manually" via `submit_step(error=True)`.
- **Destination missing entirely.** Recovery agent: confirm with the user (via plan-time prompt mechanism, P1-06) and ensure the DDL has `CREATE TABLE IF NOT EXISTS`.
- **Runtime role doesn't exist in `<dest>`'s account.** Recovery agent: surface "create role TRANSFORMER_PROD first" with the suggested SQL — does not auto-create roles (account-level operation, out of Pillar 1's scope).

### 2. DDL apply failures (Phase 2 — partial writes possible)

When a statement in `targets/<Y>/snowflake/<name>.sql` fails mid-apply, the recovery agent inspects the failure, the SQL, and the destination's current state to propose a fix:

- **Insufficient privileges on a parent object.** E.g., `CREATE TABLE` succeeds but `GRANT … ON TABLE` fails because the deploy role lacks `OWNERSHIP` of the schema. Recovery agent: surface clear "GRANT OWNERSHIP ON SCHEMA … TO ROLE <deploy_role>" suggestion to the user (refuses to auto-execute this — privilege grants on roles are too sensitive for AI auto-execution).
- **Pre-req object missing.** E.g., `CREATE TABLE … REFERENCES ANALYTICS.STAGING.FOO` fails because the parent `ANALYTICS.STAGING` schema doesn't exist. Recovery agent: edit the DDL file to add `CREATE SCHEMA IF NOT EXISTS ANALYTICS.STAGING;` before the failing statement; commit the edit to the working tree; retry from the failing statement.
- **Snowflake-side syntax issue.** E.g., agent emitted DDL that's valid for an older Snowflake version but rejected by the target's account. Recovery agent: edit the SQL to a portable form, retry.
- **Statement order issue.** E.g., GRANT runs before the table exists due to mis-ordered file. Recovery agent: re-order statements, retry from the start (idempotent statements before the fix-point are no-ops).

DDL apply is **partial-failure-possible**: if statement 3 of 5 fails, statements 1-2 are already in Snowflake. The recovery agent's edits are designed to land idempotent fixes that re-running the whole DDL file safely re-applies — and the deploy retries from the failing statement, not from scratch, so successful prior statements aren't re-executed unnecessarily within a single recovery attempt.

If recovery exhausts its budget, deploy exits non-zero with: the failing statement, Snowflake's error, the recovery agent's last attempt's diagnosis, and a clear next-step ("inspect targets/<Y>/snowflake/<name>.sql, fix manually, re-run carve el deploy"). Idempotent DDL means re-running the deploy after manual fixes is safe.

### 3. Smoke-verify failures (post-DDL)

When the post-DDL `verify` step detects drift (column type mismatch, missing grant, smoke-test query fails), the recovery agent attempts:

- **Missing grant.** Recovery agent: emit additional `GRANT … TO ROLE <runtime>` statement, append to the DDL file in the working tree, retry the GRANT against the target.
- **Smoke-test query fails on a transient issue** (network, brief permission propagation delay). Recovery agent: retry the query after a short backoff before declaring failure.
- **Column drift.** Recovery agent: usually unrecoverable here — the DDL was applied but the destination doesn't match what verify expects. Likely points to a build-vs-target mismatch the user needs to investigate. Surface diagnosis.

### Why recovery extends to Phase 2 / DDL apply

The earlier framing was "Phase 1 only — no prod writes." The user explicitly extended this: DDL apply *does* write to prod, but those writes are idempotent by P1-06's contract, so AI editing the DDL file and re-applying is safe. The recovery agent's edits land in the working tree and Snowflake account; re-running the deploy after a recovery success is the same as having gotten the DDL right on the first try.

The cost: each recovery attempt is one Anthropic API call (~$0.01-0.10) plus one Snowflake DDL re-attempt (one warehouse-second). Bounded by the budget; users with high-stakes prod deploys can lower the budget or pass `--no-auto-fix` (carries from M1.1-04's recovery wrapper).

## Failure modes

| Stage | Failure | Recovery |
|---|---|---|
| Validation | `<X>` or `<Y>` not defined | Exit 2 with the list of defined targets; `carve target list`. |
| Validation | Source artifact missing | Exit 2 with `"No EL artifact named '<name>' in target '<X>'. Run carve build first."` |
| Validation | No successful Build for `<name>` in `<X>` | Exit 2 with `"Run carve build first."` |
| Pre-flight | Deploy connection missing | Exit 2 with the deploy/runtime pattern docs link. |
| Pre-flight | Deploy role auth fails | Exit 2 with Snowflake's error. |
| Pre-flight | Drift detected | Recovery agent (P1-09) within budget; on exhaust, exit 2 with the drift report. |
| Copy | Working tree changes overwrite uncommitted user edits in `<Y>` | Refuse if `targets/<Y>/el/<name>/` has uncommitted changes; exit 2 with `"Commit or stash uncommitted edits in destination first."` |
| DDL apply | Statement fails | Recovery agent (P1-09) within budget; on exhaust, exit non-zero with failing SQL + Snowflake error + recovery's diagnosis. **DDL may be partially applied.** Idempotent re-run after fixing the issue is safe. |
| Verify (post-DDL) | Drift detected | Recovery agent within budget (e.g., add missing grant); on exhaust, exit non-zero with column-by-column diff. User investigates and either re-runs deploy (safe; idempotent) or fixes manually. |

The recovery agent operates in all three deploy contexts — pre-flight, DDL apply, post-DDL verify — within the same fix-attempt budget. Budget exhaust surfaces clear diagnosis; the user fixes manually and retries the deploy.

## Implementation

### File-level changes

New files:

- `src/carve/cli/commands/el/deploy.py` — the `carve el deploy` command (validation + pre-flight + copy + DDL apply + verify + record).
- `src/carve/cli/commands/el/verify.py` — the `carve el verify` command (read-only checks).
- `src/carve/core/deploy/__init__.py`
- `src/carve/core/deploy/preflight.py` — Phase 1 logic (drift detection, manifest validation, connection check).
- `src/carve/core/deploy/copier.py` — file-copy logic for promoting `<source>` → `<dest>` (uses `shutil.copytree` with overwrite-allowed; refuses if destination has uncommitted git changes).
- `src/carve/core/deploy/ddl_applier.py` — applies the DDL file via the deploy role (parses statements; runs in order).
- `src/carve/core/deploy/verifier.py` — verify logic (column comparison, grants check, optional smoke test).
- `tests/cli/commands/el/test_deploy.py`
- `tests/cli/commands/el/test_verify.py`
- `tests/core/deploy/test_preflight.py`
- `tests/core/deploy/test_copier.py`
- `tests/core/deploy/test_ddl_applier.py`
- `tests/core/deploy/test_verifier.py`
- `docs/deploy-from-ci.md` — short doc with the generic CLI-from-CI snippet.
- `docs/deploy-roles.md` — explains the deploy / runtime role pattern + recommended Snowflake setup SQL.

Modified files:

- `src/carve/cli/commands/el/__init__.py` — register `deploy`, `verify`.
- `src/carve/cli/commands/deploy.py` (existing M1.1-06 stub) — wired to `el.deploy.command` for backward compatibility, with deprecation warning. Removed in v0.2.
- `pyproject.toml` — adds `sqlparse>=0.4` runtime dep for splitting the DDL file into statements (already a dev dep for tests).

No DB migration. No new `Run` columns; `Run.kind="deploy"` already exists; `Run.target` was added in P1-02's migration `0004`.

## Tests

- `test_deploy_validates_targets_defined` — both `<X>` and `<Y>` must be defined; missing one exits 2.
- `test_deploy_refuses_same_source_and_dest` — `--from dev --to dev` exits 2.
- `test_deploy_no_build_exits_2` — source target has no successful Build for `<name>` → exit 2.
- `test_deploy_missing_deploy_connection` — `[snowflake.<dest>_deploy]` missing → clear error.
- `test_deploy_preflight_drift_invokes_recovery` — Phase 1: column type mismatch → recovery agent (mocked) called within budget.
- `test_deploy_ddl_apply_failure_invokes_recovery` — Phase 2: a DDL statement fails → recovery agent invoked, edits the DDL file, retries from the failing statement.
- `test_deploy_verify_failure_invokes_recovery` — Phase 3: missing grant detected → recovery agent appends GRANT to DDL file, retries.
- `test_deploy_recovery_unrecoverable_exits_2` — recovery exhausts budget at any stage → exit 2 with diagnosis.
- `test_deploy_recovery_disabled_with_flag` — `--no-auto-fix` (from M1.1-04) skips recovery; failures exit 2 immediately.
- `test_deploy_copies_files_to_dest_target` — `targets/dev/el/<name>/` mirrors to `targets/prod/el/<name>/`.
- `test_deploy_copies_ddl_file` — `snowflake/<name>.sql` likewise.
- `test_deploy_applies_ddl_in_order` — statements parsed and executed in file order against the deploy role.
- `test_deploy_idempotent` — re-running on an unchanged source produces no diffs and no Snowflake-side changes.
- `test_deploy_dest_uncommitted_changes_refused` — uncommitted git changes in `targets/<Y>/el/<name>/` → exit 2 before any writes.
- `test_deploy_records_deploy_run_row` — `runs` row of `kind="deploy"`, `pipeline_name=<name>`, `target=<Y>`, `target_id=<build_id>` exists post-success.
- `test_deploy_smoke_verify_failure_exits_non_zero` — DDL succeeds but verify fails → exit non-zero, `Run.status="failed"`.
- `test_verify_passes_on_correct_state` — destination matches manifest → exit 0.
- `test_verify_detects_column_drift` — destination has extra/missing column → exit non-zero with diff.
- `test_verify_runtime_role_grants_check` — runtime role missing INSERT → exit non-zero.
- `test_verify_no_smoke_test_flag` — `--no-smoke-test` skips the `SELECT 1 LIMIT 1` checks; default behavior runs them.
- `test_carve_deploy_legacy_alias_warns_and_forwards` — `carve deploy <name> --from X --to Y` prints deprecation banner and runs.

## Acceptance criteria

- `carve el deploy <name> --from <X> --to <Y>` copies files, applies DDL via the deploy role, smoke-verifies, records a deploy `Run` — all in one command.
- The user's CI/CD wraps the command however they like; Carve doesn't open PRs, manage branches, or know about Git providers.
- Idempotent: re-running `carve el deploy` on an unchanged source is safe (DDL idempotency contract from P1-06; file copy is a no-op).
- Recovery agent (P1-09) participates in all three deploy contexts — pre-flight drift, DDL-apply failures, post-DDL verify failures — within the configured fix-attempt budget. Unrecoverable issues exit 2 with diagnosis.
- `carve el verify <name> --target <target>` is read-only and separately runnable. Smoke test (`SELECT 1`) runs by default; `--no-smoke-test` opts out.
- Deploy role / runtime role pattern is documented (`docs/deploy-roles.md`) and exercised: `deploy` uses the deploy role; `verify` and `run` use the runtime role.
- One generic doc snippet at `docs/deploy-from-ci.md` shows the CLI invocation; no vendor-specific workflow files generated or shipped.
- Existing `carve deploy` from M1.1-06 stub forwards to `carve el deploy` with a deprecation banner; removed in v0.2.
- `ruff` + `mypy --strict` + `pytest` stay green; new tests cover deploy + verify happy paths and at least one failure mode each.

## Files this spec produces

(Summary of File-level changes section.)

New: `deploy` + `verify` CLI commands, preflight + copier + ddl_applier + verifier modules, two short docs (deploy-from-ci + deploy-roles), 6 test files.
Modified: `el/__init__.py`, `cli/commands/deploy.py` (deprecated forward), `pyproject.toml` (`sqlparse` dep).
No DB migration (`Run.kind="deploy"` already exists; `Run.target` from P1-02's migration 0004).

## Out of scope

- PR opening, branch management, provider abstractions, GitHubProvider — explicitly **not Carve's job**. Users wrap deploy in whatever CI/CD they already operate.
- Multi-target deploy in one command (`--targets staging,prod`). Defer to later if real users hit walls.
- Cross-pillar deploy (deploying a pipeline that references multiple EL artifacts atomically). Pillar 3.
- (Recovery agent now participates in all three deploy contexts, including DDL apply and verify — moved into scope per the v0.1 design.)
- Auto-generated CI/CD workflow files. Explicitly *not* shipping these. The whole point of the lean reframe.
- Vendor-specific examples beyond a generic shell snippet. The CLI is the interface; users invoke it from their tool.
- Deploy approval workflows (Slack, manual gates, etc.). Out of scope; user's CI/CD owns governance.
- Reviewer-comment-driven autonomous fixes. Far future.
- Multi-statement / non-idempotent migrations during DDL apply. Pillar 1 keeps DDL idempotent (P1-06).

## What this enables

- **`carve el deploy` is the safe, deterministic bridge from one target to another.** Files move; DDL applies; runtime role is ready. One command, one outcome.
- **No CI/CD lock-in.** GHA, GitLab CI, Airflow, custom scripts, manual invocation — all work because Carve is just a CLI command.
- **Idempotent** so failed runs are always safely retryable.
- **Recovery agent participates throughout deploy** (pre-flight, DDL apply, verify) within a bounded budget — most transient or fixable issues self-resolve without user intervention.
- The pattern extends to Pillar 2 (`carve dbt deploy`), Pillar 3 (`carve pipeline deploy`), Pillar 4 (`carve schedule deploy`) with the same `--from X --to Y` shape and the same single-command discipline.
