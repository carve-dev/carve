# `carve deploy`: component git-promotion via a configurable handoff (files → commit → push → PR), with cross-repo linked PRs

> Delivers PROJECT_PLAN acceptance step 7 and the deploy steps in use-cases UC1/UC2/UC4. `carve deploy` promotes the code a build wrote — for one or more **components** plus the control-plane composition — into version control, by default as reviewable PR(s), via a **configurable handoff**. Decision recorded 2026-06-12 (default handoff `pr`; `gh` PR mechanism).
>
> **Revised 2026-06-16 for the control-plane model** ([../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md), [../_strategy/control-plane-reference-model.md](../_strategy/control-plane-reference-model.md)): deploy promotes **named components** (resolved per [layout](./layout.md)) + the control-plane repo, and the **cross-repo linked-PR flow ships in v0.1** (ADR resolved #4) — no longer deferred. The target DDL-readiness axis shrinks to a thin `carve target verify` (no table DDL — dlt owns that); the old `carve el deploy --from/--to` retires.

## Status

- **Status:** Drafting
- **Depends on:** [M1.1 `carve build`](../milestone-1.1-followups/) (writes the files this verb promotes, and the `manifest_json.files` list it stages), [layout](./layout.md) (the `[components.<name>]` model + name→repo resolver + workspace cache this verb promotes through), [dlt-engineer EL agent](./dlt-engineer.md), [pipelines](./pipelines.md) (the pipeline DAG that orders cross-repo merges ingest-first; per-component `ref` pins).
- **Soft-depends on:** [rest-api REST/auth](./rest-api.md) — creates the `tokens` table this spec attributes deploys to. The git-promotion core builds and runs **without** rest-api; only the auth gate and the `triggered_by_token_id` FK require it (forward-declared nullable until then).
- **Blocks:** the deploy steps in use-cases UC1/UC2/UC4 (they assume PR-opening). (`cli-reference.md` in [reference-docs](./reference-docs.md) already cites this spec.)
- **Relates to:** [rest-api REST](./rest-api.md) / [mcp-server MCP](./mcp-server.md) (expose `deploy` at parity), [runtime](./runtime.md) (the optional event bus deploy emits onto).

## Goal

`carve deploy <pipeline>` promotes the code a `carve build` already wrote — across the **component repos** it touched and the **control-plane repo** (composition + component pins) — into version control, by default as reviewable pull request(s), while letting a team declare in config how far Carve takes it. Concretely:

- A **simple-mode** user (everything in one repo) runs `carve deploy salesforce` and gets one pushed branch + one PR, zero config.
- A **multi-repo** user whose change spans a `separate-remote` component repo + the control-plane repo gets **linked PRs** (one per repo), cross-linked with an explicit merge-order note — and merges them in order. Carve opens and links; the human merges.
- A team with its own CD sets `[deploy] handoff = "push"` (or `commit`, or `files`) and Carve stops at that boundary on every leg.

Carve owns the **host-agnostic core** (commit → push) and delegates PR creation to a configurable command. It never merges, never waits on a merge, and never restarts the server — rollout is the user's CI on merge (Carve ships a recommended template). The OSS ceiling is `pr`; hosted adds managed merge → rollout + approvals.

### Two axes: git promotion (this spec) vs target readiness

`deploy` is **git promotion** — moving target-agnostic *code* into version control. It is distinct from **target readiness** — making a destination able to receive runs.

```
carve build writes files (into one or more component dirs + the control-plane repo)
   │   ───────────── git-promotion axis (THIS SPEC) ─────────────
   ├─ per touched repo:  commit → push → open PR (linked + ordered across repos)
   │   ───────────── never Carve's job in OSS ─────────────
   ├─ (human) merge, in the stated ingest-first order
   └─ (user CI on merge) roll out: restart carve serve     ◄ recommended template, Carve does not run it
       └─ (CI) target readiness                            ◄ thin `carve target verify`: role/grants/connectivity/smoke
```

Under the dlt-backend positioning, **dlt owns destination table schema** (created/evolved on first run — ARCHITECTURE §7.5: "No DDL is applied"). So the old `carve el deploy --from/--to` DDL-apply machinery (`src/carve/core/deploy/{copier,ddl_applier}`) is largely vestigial and **retires**; its still-useful kernel (`preflight` + `verifier`) shrinks to a thin **`carve target verify`** (does the loader role exist, are grants in place, can we connect, smoke-test) run in CI post-merge. Speccing `carve target verify` is a small follow-up; this spec only declares the retirement so the two "deploy" verbs stop colliding.

## Reconciliation with ARCHITECTURE §7.5 + §9.4

ARCHITECTURE specifies a `Deploy` flow (§7.5) and `deploys` table (§9.4) from the pre-positioning design. **This spec supersedes both** (listed as a MODIFY):

- **`--mode pr|direct` → `--handoff files|commit|push|pr`.** Old `direct` (direct-to-prod) was hosted-only; the handoff enum is strictly richer and the OSS ceiling is `pr`.
- **Status `{opened, merged, failed, cancelled}` → `{succeeded, degraded, failed}`.** OSS does **not** track PR merge state (the CD-engine boundary); `handoff_reached` records where a leg stopped. `merged_at` dropped.
- **`linked_deploy_id` is now USED** (not reserved): it joins the per-repo `deploys` rows of one cross-repo deploy. The separate-remote dual-PR flow ships in v0.1.
- **`config_hash` drift refusal (exit 4) KEPT** (§7.6) — a safety check, not CD.
- **PR mechanism neutralized** — §7.5's "GitHub MCP" → the configured `pr_command` (default `gh`).

## Out of scope

- **Target readiness** (`carve target verify`: role/grants/connectivity/smoke). This spec declares the `carve el deploy` retirement + the verify direction but specs `carve target verify` as a small follow-up; **no table DDL** lives here (dlt owns it).
- **Merge and rollout.** Carve never merges a PR, never waits on a merge, never restarts the server. Rollout is the user's CI on merge (recommended template in `docs/`). Hosted adds managed merge → rollout.
- **Cross-repo *transaction* / atomic merge / merge-state tracking.** Carve opens + links + orders the PRs; it does not enforce the order, wait for merges, or roll back a half-merged set. That's the human/CI's job and the CD-engine line we don't cross.
- **A multi-provider `Provider` abstraction.** Rejected (archived M2-14 as too prescriptive). PR creation rides one configurable command; GitLab/Bitbucket via override.
- **GitHub App / OAuth provider auth.** The `pr` step shells out to the user's already-authenticated provider CLI; Carve owns no provider credentials.

## Files this spec produces

```
src/carve/cli/commands/deploy.py                     # REPLACE — the deprecated --from/--to alias becomes the real `carve deploy <pipeline>` promote verb
src/carve/core/deploy/git_promote.py                 # NEW — per-repo commit / push / pr stage machinery (subprocess git + configurable pr_command)
src/carve/core/deploy/cross_repo.py                  # NEW — detect touched repos from the build manifest + component resolution; open + link + order the per-repo PRs; pin-bump
src/carve/core/deploy/branch.py                      # NEW — slug + branch naming, git-ref-name validation, numeric-suffix collision
src/carve/core/deploy/templates/commit_message.j2    # NEW
src/carve/core/deploy/templates/pr_body.md.j2        # NEW — includes the cross-link + merge-order block when part of a linked deploy
src/carve/core/config/schema.py                      # MODIFY — add DeployConfig (`[deploy]` + `[deploy.targets.<name>]`)
src/carve/core/state/models.py                       # MODIFY — add the Deploy entity (per-repo rows joined by linked_deploy_id)
src/carve/core/state/repositories/deploys.py         # NEW — Deploy repository (create, link, get-latest-open-for-pipeline+component)
migrations/versions/00NN_deploys.py                  # NEW — `deploys` table. Set `down_revision` to the current Alembic head at build time.
specs/ARCHITECTURE.md                                # MODIFY — supersede §7.5/§9.4 (handoff, status, linked_deploy_id used); §10 topology→components; note el-deploy retirement
docs/deploy.md                                        # NEW — the handoff model, the cross-repo linked-PR flow, per-persona setup, the recommended CI rollout template
tests/unit/test_deploy_handoff.py                    # NEW
tests/unit/test_deploy_branch.py                     # NEW
tests/unit/test_deploy_pr_command.py                 # NEW
tests/integration/test_deploy_git_promote.py         # NEW — single-repo commit/push against a bare remote; pr_command stubbed
tests/integration/test_deploy_cross_repo.py          # NEW — linked PRs across a control-plane repo + a separate-remote component repo
```

## Behavior

### CLI surface

```
carve deploy <pipeline> [--target <name>] [--handoff <stage>] [--amend] [--yes] [--draft]
carve deploy --reconcile-pins <pipeline>     # re-bump component pins to current branch-HEAD after merge (see cross-repo)
```

- `<pipeline>` — the pipeline whose built code to promote. Deploy inspects the build manifest + component resolution to find which repos were touched.
- `--target <name>` — selects the `[deploy.targets.<name>]` handoff override + labels the PR(s); defaults to `default_target`. Deploy promotes *code* (target-agnostic); the target only selects the override + messaging.
- `--handoff <stage>` — per-invocation override (`files`|`commit`|`push`|`pr`), applied to every repo leg.
- `--amend` — extend the pipeline's most-recent open Carve branch(es) instead of opening new ones.
- `--yes` — skip the (informational) confirmation prompt, for CI.
- `--draft` — open PR(s) as drafts.

The deprecated root-level `carve deploy --from/--to` alias is **removed** (its DDL-readiness behavior retires; see *Two axes*).

### Config — the `[deploy]` block (in `carve.toml`)

```toml
[deploy]
handoff       = "pr"          # files | commit | push | pr   (default pr)
remote        = "origin"      # control-plane repo's git remote
base_branch   = "main"        # PR base; when unset, resolved from the remote's default branch
branch_prefix = "carve/"
draft_pr      = false
reuse_open_pr = false
pr_command    = "gh pr create --base={base} --head={branch} --title={title} --body-file={body_file} {draft_flag}"

[deploy.targets.prod]
handoff = "push"              # optional per-target override
```

Every field has a default, so `[deploy]` may be omitted (the greenfield wedge). Precedence: `--handoff` > `[deploy.targets.<target>]` > `[deploy]` > the `pr` default. A `separate-remote` component's own remote/base come from its `[components.<name>]` block (spec 03); the handoff level applies to every leg.

### The handoff levels (per repo leg)

Carve runs every stage **up to and including** the configured handoff, then stops. Degradation is graceful and announced — never silent.

| `handoff` | Carve does… | For |
|---|---|---|
| `files` | nothing past `build` | bespoke / monorepo git flows |
| `commit` | branch off `base_branch`, stage the leg's file set, commit | review locally first |
| `push` | `commit` + `git push -u <remote> <branch>` | **teams with their own CI/CD** |
| `pr` *(default)* | `push` + open a PR via `pr_command` | greenfield / "handle it all" |

Degradation (each announced): `pr` with no remote → `commit` (`status=degraded`); `pr` with `pr_command` binary absent/failed → `push` + manual-PR instruction (`degraded`; a failed PR step never fails the deploy — code is safely pushed); `commit`/`push` with no git repo → exit 1.

### Single-repo deploy (simple mode / separate-local)

When the build touched only one working tree (simple mode, or all components `same-repo`/`separate-local`), deploy is one PR: stage the build's declared file set, branch, push, open one PR. This is the common path and the v0.1 acceptance flow.

### Cross-repo deploy (linked PRs)

When a coordinated change spans the control-plane repo **and** one or more `separate-remote` component repos, `carve deploy` opens **one PR per repo**, links them, and states the merge order.

**What spans repos:** the build touched a `separate-remote` component's code (a dlt component's `el/` files, or a brownfield dbt component's models/`sources.yml`) **and** the control-plane repo (the `pipelines/<name>.toml` composition and/or a `[components.<name>]` pin bump in `carve.toml`).

The flow (`cross_repo.py`):

1. **Detect touched repos.** From the build manifest + `resolve_component` (spec 03), partition the changed files by the repo each resolves into: each `separate-remote` component's own remote, and the control-plane remote (composition + `carve.toml`).
2. **Per-repo PRs.** Run the handoff (commit → push → pr) against *each* touched remote — a component-code PR per separate-remote component (committed into its workspace clone, whose `origin` is that repo), and the control-plane PR for composition + pin changes.
3. **Pin bump (pinned components only).** If a touched component carries a `ref` pin (spec 03), the control-plane PR bumps `[components.<name>] ref` to the component PR's head commit. Branch-tracking components need **no** bump — merging the component PR advances the branch HEAD the control plane already follows.
4. **Link + order.** Every PR body cross-links the others and carries an explicit **merge-order** note computed from the pipeline DAG (ingest-first): merge upstream **component** PRs first (their code, and after a run their loaded columns, must exist), then the **control-plane** PR last (it references the now-merged refs). `linked_deploy_id` joins the per-repo `deploys` rows to the control-plane (lead) row.
5. **Carve opens + links + advises — it does not merge or enforce.** The human merges in order; each repo's branch protection / CI gates independently. No waiting, no merge-state polling, no cross-repo rollback. A failed PR step on one leg degrades that leg (`status=degraded`) and the ordering note tells the user what to finish manually; the other legs' PRs still open.

**Pin-to-merged-commit caveat.** A pinned bump targets the component PR's *head* commit. If the component repo **squash-merges** (rewriting the SHA), the bumped pin won't match the merged commit. v0.1 handles this two ways: (a) the PR body recommends a SHA-preserving merge (merge-commit or rebase) for Carve-linked component PRs; (b) `carve deploy --reconcile-pins <pipeline>` re-bumps pins to the components' current branch-HEAD commits after merge. Squash-without-reconcile is flagged in the PR body. (See open questions — branch-tracking components sidestep this entirely.)

### Staging the build's file set (not `git add -A`)

Each repo leg stages **exactly the files the build declared for that repo** — the `manifest_json.files` list (`builder.py` writes `manifest={"files": files_written_rel}`), partitioned by resolved repo. Confinement guards: each entry must `realpath`-resolve **inside** the target repo's tree (absolute/`..` entries refused); `.env` and anything matching `.gitignore` is never staged; a conflicting uncommitted edit to one of the build's own files aborts that leg with the file named (unrelated dirty files untouched).

### Branch naming + ref safety

- **Slug:** `re.sub(r'[^a-z0-9]+', '-', goal.lower()).strip('-')[:50]`; empty → `deploy`. Never leads with `-` or contains a git-illegal char.
- **Branch:** `carve/<plan_id>-<slug>` (suffix `-2`, `-3` on collision). Validated against `^carve/[a-z0-9][a-z0-9/-]*$` and `git check-ref-format` before any git command. The same branch name is used on every linked leg so the set is recognizable.
- **Commit message** (`commit_message.j2`): `[carve] <goal>`; body carries `Plan`, `Build`, the changed-file list, Carve version, and `Investigation` when from a recovery flow.
- **PR body** (`pr_body.md.j2`): goal, plan/build IDs, file diff summary, the investigation/failed-run link when set, and — for a linked deploy — the **cross-link + merge-order block**.

### The PR step — a single configurable command (the bounded escape hatch)

The `pr` stage runs **one configurable command**; placeholders (`{base}`, `{branch}`, `{title}`, `{body_file}`, `{draft_flag}`) are substituted and executed safely: template `shlex.split` **once**, `shell=False` (closes `;`/`&&`/`$()`); each substituted value guarded against **argv option-injection** (a value beginning with `-` is refused; the default uses `--flag=value` equals form; `{branch}` is the sanitized slug); binary absent on `PATH` → degrade to `push`. **No `pr_command` output is persisted** — only the parsed PR URL → `deploys.pr_url`; raw stdout/stderr goes to the terminal, never the state store. Default mechanism is **`gh`** (resolved 2026-06-12); GitLab via `pr_command = "glab mr create …"`.

### Git subprocess safety

`git_promote.py` mirrors `src/carve/core/deploy/copier.py`: every git call is a **list of args, `shell=False`, `cwd` pinned** to the resolved repo root, **bounded timeout**. Ref/branch values are the Carve-generated validated `carve/...` names; `remote` comes from config (control-plane) or the `[components.<name>]` block (component legs).

### The Deploy entity + events

Each repo leg of a deploy persists a `deploys` row (supersedes ARCHITECTURE §9.4):

```
deploys(
  id PK UUID, build_id FK, pipeline FK, target,
  component NULL,                   -- the separate-remote component this leg promotes; NULL = the control-plane repo (composition/pins)
  handoff_reached,                  -- files | commit | push | pr  (where this leg stopped)
  status,                           -- succeeded | degraded | failed
  branch NULL, commit_sha NULL, pr_url NULL,
  ref_bumped NULL,                  -- control-plane leg only: the [components.<name>] ref this PR bumps a pinned component to
  merge_order INT NULL,             -- advisory ingest-first position within a linked set (1 = merge first)
  file_diffs JSONB,
  config_hash,
  linked_deploy_id FK NULL,         -- joins the per-repo legs of one cross-repo deploy (points at the control-plane lead row); NULL for a single-repo deploy
  triggered_by_token_id NULL,       -- forward-declared; FK to tokens when rest-api lands
  investigation_id NULL,            -- forward-declared; FK when the Investigation entity lands
  tenant_id BIGINT NOT NULL DEFAULT 1,
  created_at
)
```

- A **single-repo** deploy is one row (`component=NULL`, `linked_deploy_id=NULL`). A **cross-repo** deploy is N rows: one control-plane lead (`component=NULL`) + one per touched component (`component=<name>`, `linked_deploy_id` → the lead). `merge_order` carries the advisory ingest-first sequence.
- `triggered_by_token_id` / `investigation_id` are **nullable, forward-declared** (FKs added when `tokens`/`Investigation` land); the migration omits those FK constraints until then. `tenant_id` follows the `BIGINT NOT NULL DEFAULT 1` convention (07/09/12), indexed leading.
- `Build.{commit_sha, pr_url, deployed_at}` (legacy `carve el deploy` columns) are left as-is and not double-written; the `deploys` rows are authoritative for `carve deploy`.
- Emits `deploy.committed` / `deploy.pushed` / `deploy.pr_opened` / `deploy.degraded` / `deploy.linked` (cross-repo set opened) onto the event bus when present ([runtime](./runtime.md)); the `deploys` rows are the source of truth, so deploy works before the bus lands.

### Re-deploying: amend vs. new PR

Default: each deploy opens **new** branch(es) + PR(s). `--amend` (or `[deploy] reuse_open_pr`): per leg, look up the most-recent non-`failed` deploy row for `(pipeline, component)`, re-validate the stored `branch` against `^carve/[a-z0-9/-]+$` and that the ref exists, then check out + commit + push (updating whatever PR points at it). No merge-state query; a merged/deleted branch falls back to a fresh branch with a note. Amend is opt-in because it silently rebases under a mid-review reviewer.

### Permissions

Triggering a deploy requires **repo write** on each touched remote (independent of Carve auth). When rest-api is present, deploy also requires a token whose `scopes` include `deploy` (or `*`); the OSS default token carries `["*"]`. The `member`/`read-only` use-cases language maps to "has/lacks the `deploy` scope" — there is no `role` column. Absent auth, the deploy attributes to the OSS single-user default and `triggered_by_token_id` is `NULL`.

### Config-hash drift

Per ARCHITECTURE §7.6, deploy computes the build's `config_hash` and **refuses (exit 4)** if config drifted from what the build was created against. Recorded on each `deploys` row.

## Tests

- **Unit** (`test_deploy_handoff.py`): `files`/`commit`/`push`/`pr` each behave per the table; `commit` stages **only** the build's declared files (unrelated dirty file untouched; out-of-root/`.env` entry refused); degradation (no remote → `commit`+`degraded`; absent `pr_command` → `push`+`degraded`); precedence `--handoff` > per-target > `[deploy]` > default.
- **Unit** (`test_deploy_branch.py`): goal with `~^:?* /\`+unicode → clean `[a-z0-9-]` slug passing `git check-ref-format`; empty → `deploy`; collision → `-2`/`-3`.
- **Unit** (`test_deploy_pr_command.py`): `;`/`&&`/`$()` template never reaches a shell; a `{title}`/`{base}` value beginning with `-` is refused / rendered `--flag=value`; unknown `handoff` rejected.
- **Integration** (`test_deploy_git_promote.py`): single-repo `carve build` → `--handoff push` produces the expected branch on a bare remote; `--handoff pr` with a fake `gh` opens the PR with the right base/head/title/body-file; git calls `cwd`-pinned + timed out.
- **Integration** (`test_deploy_cross_repo.py`): a build touching a `separate-remote` component repo + the control-plane repo opens **two linked PRs** (component + control-plane); the control-plane PR **bumps a pinned component's `ref`** to the component PR head; both PR bodies cross-link and carry the ingest-first `merge_order`; `deploys` rows are linked via `linked_deploy_id`. A **branch-tracking** component opens its PR with **no** pin bump. A failed PR step on the component leg degrades that leg while the control-plane PR still opens. `--reconcile-pins` re-bumps to current branch-HEAD.
- **State**: rows persist correct `handoff_reached`/`status`/`pr_url`/`config_hash`/`component`/`merge_order`/`linked_deploy_id`; `pr_command` stdout absent from rows; `config_hash` drift → exit 4; `investigation_id` recorded (UC4/UC5).
- **Auth (conditional on rest-api):** a token lacking `deploy` scope is refused; default `["*"]` succeeds; `triggered_by_token_id` recorded. Skipped (attributes to default identity) when 09 absent.

## Acceptance

- **Single-repo wedge:** `carve build <plan>` → `carve deploy salesforce` with no config → one `carve/<plan>-<slug>` branch pushed + one PR via `pr_command`; one `deploys` row (`handoff_reached=pr`, `status=succeeded`); a `deploy.pr_opened` event when the bus is present.
- **Cross-repo linked PRs (v0.1):** a coordinated change spanning a `separate-remote` component repo + the control-plane repo opens **linked PRs** (one per repo), cross-linked with an ingest-first merge-order note; pinned components get a `ref` bump in the control-plane PR, branch-tracking ones don't; `linked_deploy_id` joins the rows. Carve opens and links; it does **not** merge or wait.
- **Team with CD:** `[deploy.targets.prod] handoff = "push"` → every leg pushes a branch + prints the compare URL; no provider call.
- **Full hand-off:** `handoff = "files"` → Carve touches no git.
- **Graceful degrade:** absent `pr_command` binary → push + manual-PR instruction; `status=degraded`, not failed; other legs unaffected.
- **No provider lock-in:** a GitLab team gets working MR(s) via `pr_command = "glab mr create …"`, no code change.
- **Drift safety:** a drifted build → exit 4 before touching git.
- **Consistency:** use-cases UC1/UC2/UC4 deploy steps hold; `cli-reference.md` cites this spec.

## Design notes

- **Why configurable handoff?** `files`/`commit`/`push`/`pr` are points on one line; config picks the point. Newcomer gets the full wedge with zero config; the team with CD sets one line. Flexibility without a strategy-plugin system.
- **Why ship linked-PR cross-repo deploy in v0.1 (not defer)?** It's the brownfield separate-repo story — the bigger adoption prize — and it's what graduation surfaces. Crucially it's *not* a CD engine: Carve **opens + links + orders**, the human merges. The hard parts we explicitly do **not** do (merge, wait, enforce order, roll back) are the CD-engine line. Name-based indirection + per-component pins (spec 03/08) make the control-plane PR a clean pin-bump rather than a content rewrite.
- **Why the OSS ceiling is `pr` (no merge/rollout)?** Narrow runtime, not a CD engine. Merge is human/branch-protection; rollout is the user's CI (recommended template). This also draws the OSS/hosted line: hosted's "push-button deploy + approvals" is the managed merge→rollout layer.
- **Why retire `carve el deploy`'s DDL-apply?** dlt owns destination table schema (ARCHITECTURE §7.5: no DDL applied). The remaining real job — the destination *container* (role/grants/connectivity) dlt assumes pre-exists — shrinks to a thin `carve target verify`, out of the "deploy" namespace so two verbs stop colliding.
- **Why reference + pin components rather than vendoring them?** This is the control-plane model: deploy promotes a component's *own* repo and bumps a pin in the control plane. It mirrors how the runtime resolves components, and it's what makes the linked-PR set coherent (one pin-bump PR ties the set together).
- **Why stage the build's file set, not `git add -A`?** Never sweep unrelated edits (or a local `.env`) into a deploy commit.
- **Why subprocess git?** The user's git config (signing, hooks) is respected. Mirrors `copier.py`.

## Open questions

> Tagged per the scheme in spec 01.

- **Pin-to-merged-commit under squash-merge.** *Implementation default, with a known edge.* A pinned bump targets the component PR head; squash-merge rewrites the SHA so the pin won't match. v0.1: recommend SHA-preserving merge for linked component PRs **and** ship `carve deploy --reconcile-pins`. Branch-tracking components avoid the issue entirely (no pin). Revisit a tag-based or merge-webhook reconcile post-v0.1 if squash shops hit friction.
- **Per-leg handoff override.** *Implementation default.* v0.1 applies one `handoff` to all legs. A future `[components.<name>] deploy_handoff` could let a component repo with its own CD use `push` while the control-plane repo uses `pr`. Defer until asked.
- **Auth model: scopes, not roles.** *Resolved, pending confirm.* Gate on a `deploy` scope (rest-api), not the use-cases `member`/`read-only` narrative; `triggered_by_token_id` forward-declared.
- **`pr_command` injection surface.** *Implementation default.* `shlex.split` once, `shell=False`, leading-`-` values refused, `--flag=value` form. Locked in tests.
- **CI rollout template — ship or docs-only.** *Implementation default.* A documented `.github/workflows/carve-rollout.yml` in `docs/deploy.md`, not a generated artifact.
- **`carve target verify` scope.** *Follow-up.* This spec declares the `carve el deploy` retirement + the verify direction (role/grants/connectivity/smoke, no table DDL); the actual `carve target verify` command is a small separate spec.
