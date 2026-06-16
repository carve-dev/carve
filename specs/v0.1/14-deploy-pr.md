# v0.1-14 — `carve deploy`: git promotion via a configurable handoff (files → commit → push → PR)

> Delivers PROJECT_PLAN acceptance step 7 ("`carve deploy <pipeline>` — opens a PR with the new files") and the deploy steps in use-cases UC1/UC2/UC4. Resolves UC1's deploy-ownership boundary ("`carve deploy` opens a PR; it does **not** push code to the central server", [use-cases.md:101](../use-cases.md)) and UC2's amend-vs-new-PR question ([use-cases.md:235](../use-cases.md)) via a **configurable handoff**: Carve owns the host-agnostic core (commit → push) and opens a PR by default, while teams with their own CD declare how far Carve goes. Decision recorded 2026-06-12; default handoff is `pr`.

## Status

- **Status:** Drafting
- **Depends on:** [M1.1 `carve build`](../milestone-1.1-followups/) (writes the files this verb promotes, and the `manifest_json.files` list it stages), [v0.1-03 flat-layout](./03-flat-layout.md) (what is committed + repo topology), [v0.1-04 EL agent](./04-el-agent-dlt.md) (produces the build whose file set is staged).
- **Soft-depends on:** [v0.1-09 REST/auth](./09-rest-api.md) — creates the `tokens` table this spec attributes deploys to. The git-promotion core builds and runs **without** v0.1-09; only the auth gate and the `triggered_by_token_id` FK constraint require it (forward-declared nullable until then). If v0.1-14 lands first, deploys attribute to the OSS single-user default identity (`owner_user_id`/default token).
- **Blocks:** the deploy steps in use-cases UC1/UC2/UC4 (they assume PR-opening). (`cli-reference.md` in [v0.1-13](./13-reference-docs.md) already cites this spec — nothing to fix there.)
- **Relates to:** [v0.1-09 REST](./09-rest-api.md) / [v0.1-10 MCP](./10-mcp-server.md) (expose `deploy` at parity), [v0.1-07 runtime](./07-runtime.md) (the optional event bus deploy emits onto), `carve el deploy` (the **other** deploy axis — target DDL-readiness — which this spec does not touch).

## Goal

`carve deploy <pipeline>` promotes the files `carve build` already wrote into version control and, **by default, opens a reviewable pull request** — while letting a team declare, in config, how far Carve takes it. Concretely:

- A greenfield user runs `carve deploy salesforce` and gets a pushed branch + an open PR against the base branch, with zero extra config.
- A team with an existing CD pipeline sets `[deploy] handoff = "push"` (or `commit`, or `files`) and Carve stops at that boundary, handing off cleanly to their process.
- Carve owns the **host-agnostic core** (commit → push) and **delegates PR creation to a configurable command**. It never owns merge or rollout — those are the user's CI on merge (Carve ships a recommended template). The OSS ceiling is `pr`; the hosted product adds managed merge → rollout + approvals on top.

This is the "one prompt spans ingest + transform + deploy-via-PR" wedge — made configurable so it meets teams where their existing process already is.

### What "deploy" is and isn't (two orthogonal axes)

`deploy` decomposes into a linear stage pipeline. **This spec owns only the git-promotion axis.** Target DDL-readiness is a separate axis that already ships as `carve el deploy` / `src/carve/core/deploy/` and is explicitly out of scope here.

```
carve build writes files into the working tree
   │   ───────────── git-promotion axis (THIS SPEC) ─────────────
   ├─ commit   → branch off base_branch, stage the build's file set, commit
   ├─ push     → push the branch to the remote            ◄ host-agnostic core ends here
   ├─ pr       → open a PR via the configured pr_command  (needs a provider tool)
   │   ───────────── never Carve's job in OSS ─────────────
   ├─ (human) merge
   └─ (user CI on merge) roll out: restart carve serve    ◄ recommended template, Carve does not run it
       └─ (CI, target creds) target DDL-readiness         ◄ separate axis: `carve el deploy`
```

## Reconciliation with ARCHITECTURE §7.5 + §9.4

ARCHITECTURE already specifies a `Deploy` flow (§7.5) and `deploys` table (§9.4) from the pre-positioning design. **This spec supersedes both**, and lists `ARCHITECTURE.md` as a MODIFY so they stop conflicting. The changes and why:

- **`--mode pr|direct` → `--handoff files|commit|push|pr`.** The old `direct` (direct-to-prod, no PR) was hosted-only; in the handoff model the OSS ceiling is `pr` and hosted's direct-to-prod becomes part of its managed merge/rollout layer. The handoff enum is strictly richer.
- **Status enum `{opened, merged, failed, cancelled}` → `{succeeded, degraded, failed}`.** The old enum tracked PR *merge* lifecycle (`merged`); OSS deliberately does **not** track merge (that's the CD-engine boundary). The new status records the deploy *action* outcome; `handoff_reached` records where it stopped. `merged_at` is dropped.
- **`config_hash` drift refusal (exit 4) is KEPT** (ARCHITECTURE §7.6). It is a safety check, not a CD-engine feature — `carve deploy` refuses a build whose config has drifted.
- **`linked_deploy_id` is RESERVED but separate-repo dual-PR is DEFERRED** (see Out of scope). The column stays in the schema (always `NULL` in v0.1) so the future separate-repo follow-up is additive.
- **PR mechanism neutralized.** §7.5's "PR open via GitHub MCP" and use-cases UC1 step 22's "via GitHub MCP" are changed to "via the configured `pr_command`" — the mechanism is configurable and its default is an open question (below), so the corpus must not hard-code MCP.

## Out of scope

- **Target DDL-readiness** (apply DDL + smoke-verify on a destination). That is the existing `carve el deploy` verb and the `src/carve/core/deploy/{preflight,copier,ddl_applier,verifier}` machinery. The P1.1-03 reframe of `carve el deploy` around single-target git-promotion is a separate follow-up, not this spec.
- **Separate-repo (separate-remote) dual-PR deploy.** When `[dbt]`/`[dlt]` topology is `separate-remote` ([v0.1-03](./03-flat-layout.md)), a build can touch a second repo (e.g. a `sources.yml` patch against a separate dbt repo). v0.1-14 covers **single-working-tree** deploy (same-repo and separate-local, where all files live in one tree). The dual-linked-PR path (two `deploys` rows via `linked_deploy_id`, per ARCHITECTURE §7.5/§10.6) is a **named follow-up**; `carve deploy` against a `separate-remote` topology errors with a clear "separate-remote deploy not yet supported; push the second repo manually" message rather than silently half-deploying.
- **Merge and rollout.** Carve never merges a PR and never restarts the server as a deploy stage. Rollout is the user's CI on merge; this spec ships a *recommended* CI template in `docs/`, it does not execute one. (Hosted adds managed merge → rollout — out of scope for OSS.)
- **A multi-provider `Provider` abstraction.** Deliberately rejected (it got M2-14 archived as "too prescriptive for OSS"). PR creation rides a single configurable command; GitLab/Bitbucket are supported by overriding that command, not a built-in provider class.
- **GitHub App / OAuth provider auth.** The `pr` step shells out to the user's already-authenticated provider CLI; Carve owns no provider credentials or tokens.

## Files this spec produces

```
src/carve/cli/commands/deploy.py                     # REPLACE — currently the deprecated --from/--to alias to `carve el deploy`; becomes the real `carve deploy <pipeline>` promote verb
src/carve/core/deploy/git_promote.py                 # NEW — the commit / push / pr stage machinery (subprocess git + configurable pr_command)
src/carve/core/deploy/branch.py                      # NEW — slug + branch naming (`carve/<plan_id>-<slug>`), git-ref-name validation, numeric-suffix collision
src/carve/core/deploy/templates/commit_message.j2    # NEW — structured commit message
src/carve/core/deploy/templates/pr_body.md.j2        # NEW — structured PR body
src/carve/core/config/schema.py                      # MODIFY — add DeployConfig (`[deploy]` + `[deploy.targets.<name>]`) to Config
src/carve/core/state/models.py                       # MODIFY — add the Deploy entity
src/carve/core/state/repositories/deploys.py         # NEW — Deploy repository (create / get-latest-open-for-pipeline)
migrations/versions/0007_deploys.py                  # NEW — `deploys` table. Set `down_revision` to the current Alembic head at build time (do NOT hardcode; head may be 0006 or a later runtime/auth migration if 07/09 land first)
specs/ARCHITECTURE.md                                # MODIFY — supersede §7.5 (mode→handoff, drop merge-tracking) + §9.4 (deploys row shape); neutralize the "GitHub MCP" PR mechanism
specs/use-cases.md                                   # MODIFY — UC1 step 22: "via GitHub MCP" → "via the configured pr_command" (already applied; the only walkthrough that pinned the mechanism)
docs/deploy.md                                        # NEW — the handoff model, per-persona setup, and the recommended CI rollout template
tests/unit/test_deploy_handoff.py                    # NEW — handoff levels + degradation + config/CLI precedence
tests/unit/test_deploy_branch.py                     # NEW — slug sanitization, ref-name validation, collision suffix
tests/unit/test_deploy_pr_command.py                 # NEW — shell + argv-option injection mitigation
tests/integration/test_deploy_git_promote.py         # NEW — commit/push against a bare-repo remote; pr_command stubbed
```

## Behavior

### CLI surface

```
carve deploy <pipeline> [--target <name>] [--handoff <stage>] [--amend] [--yes] [--draft]
```

- `<pipeline>` — the pipeline whose built files to promote.
- `--target <name>` — selects which `[deploy.targets.<name>]` handoff override applies and labels the PR; defaults to the project `default_target`. (Deploy promotes *code*, which is target-agnostic; the target only selects the handoff override + messaging.)
- `--handoff <stage>` — per-invocation override of the configured handoff (`files` | `commit` | `push` | `pr`).
- `--amend` — extend the pipeline's most-recent open Carve branch instead of opening a new one (see *Re-deploying*). Default is a fresh branch + PR.
- `--yes` — skip the confirmation prompt (for CI). Deploy is low-risk (it targets a PR, not prod), so the prompt is informational.
- `--draft` — open the PR as a draft (overrides `[deploy] draft_pr`).

The deprecated root-level `carve deploy --from/--to` alias is **removed**; its DDL-readiness behavior remains available as `carve el deploy`.

### Config — the `[deploy]` block (in `carve.toml`)

A new `DeployConfig` is added to `src/carve/core/config/schema.py` as the `[deploy]` section of `carve.toml`, with optional per-target overrides:

```toml
[deploy]
handoff       = "pr"          # files | commit | push | pr   (default pr)
remote        = "origin"      # git remote to push to
base_branch   = "main"        # PR base; when unset, resolved from `git symbolic-ref refs/remotes/<remote>/HEAD`
branch_prefix = "carve/"      # all Carve branches start here
draft_pr      = false
reuse_open_pr = false         # if true, `carve deploy` defaults to --amend behavior
pr_command    = "gh pr create --base={base} --head={branch} --title={title} --body-file={body_file} {draft_flag}"

[deploy.targets.prod]         # optional per-target override
handoff = "push"              # prod has its own CD
```

- Every field has a default, so `[deploy]` may be omitted entirely (the greenfield wedge). `base_branch`/`remote` resolution: `remote` defaults to `origin`; `base_branch` falls back to the detected remote default branch when unset.
- Precedence for the effective handoff: `--handoff` flag > `[deploy.targets.<target>] handoff` > `[deploy] handoff` > the `pr` default.

### The handoff levels

Carve runs every stage **up to and including** the configured handoff, then stops and prints what to do next. Degradation is graceful and always announced — Carve never silently skips a stage.

| `handoff` | Carve does… | Stops by printing… | For |
|---|---|---|---|
| `files` | nothing past `build` | "Files written to the working tree; git is yours." | bespoke / monorepo git flows |
| `commit` | branch off `base_branch`, stage the build's file set, commit | the branch name + `git log` line | review locally before pushing |
| `push` | `commit` + `git push -u <remote> <branch>` | the pushed branch + a ready-to-use PR-compare URL | **teams with their own CI/CD + branch protection** |
| `pr` *(default)* | `push` + open a PR via `pr_command` | the PR URL | greenfield / "handle it all" |

Degradation rules (each announced, none silent):

- `handoff = pr` but no remote configured → behaves as `commit`; `status = degraded`.
- `handoff = pr` but the `pr_command` binary is absent or the call fails → behaves as `push`, prints a manual "open a PR" instruction with the compare URL; `status = degraded`. A failed PR step never fails the deploy — the code is safely pushed.
- `handoff = push`/`commit` but no git repo → error (exit 1) with a "run `git init` or set `handoff = files`" message.
- `separate-remote` topology with a cross-repo file set → error (exit 1) per Out of scope.

### Staging the build's file set (not `git add -A`)

The `commit` stage stages **exactly the files the build declared** — the `manifest_json.files` list on the Build row (`src/carve/cli/orchestrator/builder.py` writes `manifest={"files": files_written_rel}`, a sorted list of project-relative POSIX paths). This is a deliberate improvement over M2-14's `git add -A`: it never sweeps a user's unrelated working-tree edits into the deploy commit.

Confinement guards (the manifest is influenced by an LLM-driven build agent, and staging runs at project root to reach root-level `.env.example`/`sources.yml`):

- Each manifest entry MUST `realpath`-resolve to a location **inside** the project root, or staging aborts. Absolute paths and `..`-bearing entries are refused.
- `.env` (and anything matching the project `.gitignore`) is **never** staged even if it appears in the manifest. (Today the builder only ever writes `.env.example`; this is defense in depth.)
- If one of the build's own declared files has a conflicting uncommitted edit, Carve refuses with a clear message naming the file; unrelated dirty files are left untouched.

### Branch naming + ref-name safety

- **Slug:** derived from the goal via a strict allow-list — `re.sub(r'[^a-z0-9]+', '-', goal.lower()).strip('-')[:50]`; if the result is empty, fall back to the constant `deploy`. This can never lead with `-` or contain a git-illegal ref character.
- **Branch:** `carve/<plan_id>-<slug>`. Before any git command, the full ref is validated to match `^carve/[a-z0-9][a-z0-9/-]*$` and to pass `git check-ref-format` (no `..`, no trailing `.lock`, no control chars). If the branch already exists, append `-2`, `-3`, … (ported from M2-14).
- **Commit message** (`commit_message.j2`): subject `[carve] <goal summary>`; body carries `Plan: <plan_id>`, `Build: <build_id>`, the changed-file list, the Carve version, and — when the deploy came from a recovery flow — `Investigation: <investigation_id>` (UC4/UC5).
- **PR body** (`pr_body.md.j2`): goal, plan/build IDs, the file diff summary, and an auto-link back to the failed run + investigation when `investigation_id` is set.

### The PR step — a single configurable command (the bounded escape hatch)

The `pr` stage runs **one configurable command**. Placeholders (`{base}`, `{branch}`, `{title}`, `{body_file}`, `{draft_flag}`) are substituted into the command, then executed safely:

- The template is `shlex.split` **once** and the subprocess runs with **`shell=False`** — this closes the classic shell-metacharacter vector (`;`/`&&`/`$()`).
- Each substituted value is guarded against **argv option-injection**: a value that begins with `-` is refused (the only values that ever could are `{title}`/`{base}` from user/LLM text; `{branch}` is the already-sanitized slug and structurally cannot). The default `pr_command` uses the `--flag=value` equals form for substituted values so a leading-dash value can never be parsed as a separate flag. `{draft_flag}` is the single controlled placeholder allowed to expand to an argv element (`--draft` or empty).
- If `pr_command`'s binary isn't on `PATH`, Carve degrades to `push` (above) rather than failing.
- **No `pr_command` output is persisted.** Only the parsed PR URL is written to `deploys.pr_url`; raw stdout/stderr (which may echo a provider token or auth error) is surfaced to the user's terminal but never written to the `deploys` row, the state store, or persisted logs.

Default mechanism is the GitHub CLI (`gh`) — it carries the user's existing auth (incl. SSO/2FA/Enterprise), needs no PAT in `.env`, and keeps Carve out of provider-credential ownership. A GitLab team sets `pr_command = "glab mr create ..."`; a chat-driven flow can instead route through the GitHub MCP after a `handoff = push` deploy. **(`gh`-as-default is an open question — see below.)**

### Git subprocess safety

`git_promote.py` mirrors the existing safe pattern in `src/carve/core/deploy/copier.py`: every git invocation is a **list of args with `shell=False`**, **`cwd` pinned to the resolved project root**, and a **bounded timeout**. Ref/branch values are the Carve-generated, validated `carve/...` names (which cannot lead with `-`); `remote` comes from config.

### The Deploy entity + events

Each `carve deploy` persists a `deploys` row — the durable, authoritative record for the git-promotion axis (supersedes ARCHITECTURE §9.4):

```
deploys(
  id PK UUID, build_id FK, pipeline FK, target,
  handoff_reached,                  -- files | commit | push | pr  (where it stopped)
  status,                           -- succeeded | degraded | failed
  branch NULL, commit_sha NULL, pr_url NULL,
  file_diffs JSONB,                 -- the staged file set / diff summary (for the PR body + audit)
  config_hash,                      -- drift check vs the build (§7.6)
  linked_deploy_id FK NULL,         -- RESERVED; always NULL in v0.1 (separate-repo dual-PR deferred)
  triggered_by_token_id NULL,       -- forward-declared; FK to tokens added when v0.1-09 lands
  investigation_id NULL,            -- forward-declared plain UUID; FK when the Investigation entity lands
  tenant_id BIGINT NOT NULL DEFAULT 1,
  created_at
)
```

- **`triggered_by_token_id`** and **`investigation_id`** are **nullable, forward-declared** columns — plain UUID/text until the `tokens` table (v0.1-09) and the `Investigation` entity (UC4/UC5 follow-up) exist, at which point the FK constraints are added by an additive migration. The `deploys` migration does **not** define those FK constraints if the target tables don't yet exist.
- **`tenant_id`** follows the same `BIGINT NOT NULL DEFAULT 1` convention as the other v0.1 runtime/auth specs ([v0.1-07](./07-runtime.md):145, [v0.1-09](./09-rest-api.md), [v0.1-12](./12-ask-verb.md)) per ARCHITECTURE §9.9; index it with `tenant_id` leading. (The shipped M1 tables use `owner_user_id`; the corpus-wide `owner_user_id`→`tenant_id` transition is handled across these v0.1 specs. If v0.1-14 lands before them, it is the first to adopt `tenant_id` — an acceptable additive migration.)
- **Relationship to existing `Build` columns:** `Build.{commit_sha, pr_url, deployed_at}` already exist (populated by the legacy `carve el deploy` path). The git-promotion axis records its state **only** on the `deploys` row, which is authoritative for `carve deploy`; the Build columns are left as-is and not double-written.
- Deploy emits `deploy.committed` / `deploy.pushed` / `deploy.pr_opened` / `deploy.degraded` — **new event kinds introduced by this spec** — onto the event bus when present ([v0.1-07](./07-runtime.md)); registered alongside the other event kinds (ARCHITECTURE §9.7). The `deploys` row is the source of truth, so deploy is fully functional before the event bus lands.

### Re-deploying: amend vs. new PR

- **Default:** every `carve deploy` opens a **new** branch + PR (simplest; each PR is one independently-reviewable change).
- **`--amend`** (or `[deploy] reuse_open_pr = true`): Carve looks up the **most-recent non-`failed` deploy** for this pipeline in the `deploys` table, **re-validates its stored `branch` against `^carve/[a-z0-9/-]+$`** and that the ref still exists, checks it out, stages + commits the new build's file set, and pushes — which updates whatever PR already points at that branch. Carve does **not** query provider merge state (no merge-tracking); if the branch was already merged/deleted, the ref-existence check falls through to a fresh branch with a note.
- Amend is opt-in because it silently rebases content under a reviewer who may be mid-review; the safe default never does that.

### Permissions

- Triggering a deploy requires **repo write** (to push) — independent of Carve auth, exactly as use-cases UC2 states.
- **Carve auth gate (when v0.1-09 is present):** deploy requires a token whose `scopes` include `deploy` (or `*`); the OSS single-user default token carries `["*"]` and so always passes. This aligns with v0.1-09's **scope-based** token model — there is no `role` column; the `member`/`read-only` language in the use-cases narrative maps to "has/lacks the `deploy` scope." When no auth is configured (v0.1-09 not landed), the CLI path attributes the deploy to the OSS single-user default identity and `triggered_by_token_id` is `NULL`.

### Config-hash drift

Per ARCHITECTURE §7.6, `carve deploy` computes the build's `config_hash` and **refuses (exit 4)** if the current config has drifted from what the build was created against — deploying a stale build is a footgun. The hash is recorded on the `deploys` row.

## Tests

- **Unit** (`test_deploy_handoff.py`): `files` touches no git; `commit` creates a branch + a commit containing **only** the build's declared file set (an unrelated dirty file is left uncommitted; an out-of-root or `.env` manifest entry is refused); `push` pushes to a bare-repo remote; `pr` invokes a stubbed `pr_command` with the templated argv. Degradation: no remote → `commit` + `status=degraded`; absent `pr_command` binary → `push` + `status=degraded`. Precedence: `--handoff` > per-target > `[deploy]` > `pr` default.
- **Unit** (`test_deploy_branch.py`): a goal full of `~^:?* /\` + unicode yields a clean `[a-z0-9-]` slug that passes `git check-ref-format`; empty-after-sanitize falls back to `deploy`; existing-branch collision appends `-2`, `-3`.
- **Unit** (`test_deploy_pr_command.py`): a `pr_command` template with `;`/`&&`/`$()` is `shlex.split` into argv and never reaches a shell; a `{title}`/`{base}` value beginning with `-` (e.g. `--reviewer=attacker`) is refused (or rendered as `--flag=value` so it cannot be parsed as a flag); an unknown `handoff` value is rejected by `DeployConfig`.
- **Integration** (`test_deploy_git_promote.py`): against a tempdir git repo with a bare-origin remote, `carve build` → `carve deploy --handoff push` produces the expected branch on origin; `--handoff pr` with a fake `gh` on `PATH` opens the "PR" with the right base/head/title/body-file. Git calls run with `cwd` pinned and a timeout.
- **State**: a `deploys` row is persisted with the correct `handoff_reached`, `status`, `pr_url`, `config_hash`; an `investigation_id` passed through is recorded (UC4/UC5 chain); `pr_command` stdout is **not** present in the row. Config-hash drift → exit 4.
- **Amend**: with a non-`failed` deploy recorded for the pipeline, `--amend` re-validates and reuses its branch; a tampered/missing branch falls back to a fresh branch with a note.
- **Auth (conditional on v0.1-09):** a token lacking the `deploy` scope is refused; the default `["*"]` token succeeds; `triggered_by_token_id` is recorded. When v0.1-09 is absent, this test is skipped and the deploy attributes to the default identity.

## Acceptance

- **Default wedge:** a user runs `carve build <plan>` then `carve deploy salesforce` with no deploy config → a `carve/<plan>-<slug>` branch is pushed and a PR is opened against `base_branch` via the configured `pr_command`; a `deploys` row (`handoff_reached=pr`, `status=succeeded`) is recorded (and, when the event bus is present, a `deploy.pr_opened` event).
- **Team with CD:** `[deploy.targets.prod] handoff = "push"` → `carve deploy salesforce --target prod` pushes the branch, prints the compare URL, and never calls a provider tool.
- **Full hand-off:** `[deploy] handoff = "files"` → Carve writes nothing to git; the user's process owns everything from there.
- **Graceful degrade:** with no `pr_command` binary on `PATH`, `handoff = pr` pushes the branch and prints a manual-PR instruction; the deploy is recorded as `degraded`, not failed.
- **Consistency:** use-cases UC1 step 22, UC2 step 7, and UC4 step 18 hold (PR-opening is the default; the mechanism wording is `pr_command`, updated in the same change), and `cli-reference.md` documents `carve deploy <pipeline>` citing this spec.
- **No provider lock-in:** a GitLab team gets a working PR/MR flow by setting `pr_command = "glab mr create ..."` with no Carve code change.
- **Drift safety:** deploying a build whose config has drifted exits 4 without touching git.

## Design notes

- **Why a configurable handoff instead of one fixed model?** It dissolves the deploy-ownership fork: `files`/`commit`/`push`/`pr` are points on one line, and config picks the point. The newcomer gets the full wedge with zero config (`pr` default); the team with existing CD sets one line and Carve gets out of the way. Flexibility without a strategy-plugin system.
- **Why the OSS ceiling is `pr` (no merge/rollout)?** Carve is a narrow, opinionated runtime, not a CD engine. Merge is human/branch-protection; rollout is the user's CI on merge (recommended template in `docs/deploy.md`). This boundary also draws the OSS/hosted line cleanly — the hosted product's "push-button deploy + approval workflows" is exactly the managed merge → rollout layer on top.
- **Why supersede ARCHITECTURE §7.5/§9.4 rather than adopt it?** The old shape encoded merge-tracking (`merged_at`, `status=merged`) and a hosted-only `direct` mode that the narrow-runtime positioning removes from OSS. Dropping them is the *correct* scope discipline; this spec makes the supersession explicit (and a MODIFY) so the two stop conflicting.
- **Why `gh` + a configurable command, not a `pygithub` `Provider` class?** The Provider abstraction got M2-14 archived as too prescriptive for OSS. Shelling to the user's `gh` keeps Carve out of provider-auth/state ownership (incl. SSO/Enterprise/2FA), and the one command override is the *single bounded escape hatch* for GitLab/Bitbucket — not a general hook system. Consistent with dlt's idiom (`dlt deploy` generates CI; it has no PR concept).
- **Why stage the build's file set, not `git add -A`?** So a deploy never sweeps unrelated working-tree edits into its commit — a real footgun in M2-14's `add -A` — and so a local `.env` can never be committed.
- **Why a `Deploy` entity (vs. the existing `Build.{commit_sha,pr_url}`)?** Audit (who deployed, where it stopped, the PR URL, the config hash) and the UC4/UC5 recovery chain (`investigation_id` in → `resolved_by_deploy_id` out) both need a durable, per-deploy record; the Build columns are per-build, not per-deploy.
- **Why subprocess git (not `pygit2`/`dulwich`)?** The user's git is configured the way they like (signing keys, hooks); bypassing it would mean recreating all of that. Mirrors `copier.py`.

## Open questions

> Tagged per the scheme in spec 01.

- **PR-open mechanism → RESOLVED: `gh` CLI** (decided 2026-06-12). The default `pr_command` shells to `gh pr create` with a configurable override (`glab mr create` for GitLab, etc.); rationale in Design notes. `pygithub` (Carve owns a provider client + a PAT in `.env`) and routing the CLI path through the GitHub MCP were considered and rejected — `gh` carries the user's existing auth and keeps Carve out of provider-credential ownership. The corpus stays mechanism-neutral, so no further use-cases/ARCHITECTURE churn.
- **Auth model: scopes, not roles.** *Resolved, pending confirm.* The permission gate aligns to v0.1-09's `scopes: list[str]` (gate on a `deploy` scope) rather than the `member`/`read-only` roles that appear only in the use-cases narrative. `triggered_by_token_id` is a forward-declared nullable FK. Confirm this is the intended auth direction.
- **Separate-repo (separate-remote) dual-PR deploy.** *Deferred follow-up.* v0.1-14 errors clearly on `separate-remote` topology; `linked_deploy_id` is reserved for the follow-up. Confirm deferral is acceptable for v0.1's Snowflake same-repo acceptance flow.
- **`pr_command` injection surface.** *Implementation default.* Mitigation: `shlex.split` once, `shell=False`, substituted values guarded against leading-dash option injection, `--flag=value` equals form in the default. Locked in `test_deploy_pr_command.py`.
- **Ship a concrete CI rollout template, or docs-only?** *Implementation default.* v0.1 ships a documented `.github/workflows/carve-rollout.yml` example in `docs/deploy.md` (copy-paste), not a generated/executed artifact.
- **Re-deploy default = new PR, `--amend` opt-in.** *Resolved.* The safe default never rebases a reviewer's open PR; `--amend` reuses the recorded branch without querying merge state.
