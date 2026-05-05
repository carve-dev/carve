# M2-14 — GitHub PR integration (proposed rewrite)

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M1.1-06 (plan/build/run/deploy lifecycle), M2-01 (plan/deploy workflow), M2-10 (server)

## Update notes (proposal)

The original M2-14 was written before M1.1-06 landed. M1.1-06 redefined the
lifecycle verbs:

- `carve plan "<goal>"` — design only, no files. Persists a `Plan` row.
- `carve build <plan_id>` — writes `pipelines/<name>/` files locally and
  creates/updates the `Pipeline` row. Code generation happens here.
- `carve run <pipeline_name>` — executes the pipeline as it stands on disk
  via `LocalVenvRunner`. Re-runnable at will.
- `carve deploy <pipeline_name>` — **reserved for prod-deploy via PR.**
  Today it is a stub; this spec is what fills it in.

The original spec described `carve deploy` as the verb that runs the task
graph and then opens a PR with the code it generated. That premise no
longer holds: by the time `carve deploy` runs, the pipeline files already
exist on disk because `carve build` wrote them. Deploy has exactly one job:
package those files into a branch, push them, and open a PR.

This proposal therefore changes:

- The framing of what deploy does (PR creation only, not code generation).
- Branch naming (`carve/<pipeline_name>-<short_id>`, not
  `carve/<plan_id>-<slug>`), because pipelines are durable and deployed
  repeatedly while plan ids are biographical and change with every refine.
- Commit-message and PR-body templates to read as "deploy pipeline X
  (built from plan Y)" rather than "carve generated this code".
- The failure semantics: PR creation **is** the deploy. If push or PR
  creation fails, the deploy failed; the user re-runs `carve deploy
  <pipeline_name>` after fixing the underlying issue. Code is never lost
  because it was already on disk before deploy ran.
- The file map: `src/carve/cli/commands/deploy.py` becomes a real
  implementation; `src/carve/core/git/` modules carry the GitHub client
  and branch/commit/PR logic.

Everything else from the original — auth via PAT, config layout,
draft/non-draft toggle, labels, reviewers, the GitHub-only-for-v0.1
reasoning, the rich PR body — is preserved.

## Purpose

`carve deploy <pipeline_name>` promotes a pipeline from local-dev to prod
by opening a pull request against the user's repo. The PR contains the
files under `pipelines/<name>/` exactly as they sit on disk after
`carve build`. The PR description includes the plan summary, file diffs,
and impact analysis. CI runs on the PR; the user reviews; the user
merges. This is the safety review boundary between dev iteration
(`carve build` + `carve run`) and production.

Deploy does **not** invoke any agent, does **not** regenerate code, and
does **not** execute the pipeline. The code already exists; deploy just
ships it.

## Why GitHub-only for v0.1

- GitHub is dominant in the data ecosystem; covers ~80% of users
- GitLab support adds complexity (different API, OAuth flow); save for v0.2
- Bitbucket usage in data tooling is small; defer
- Self-hosted Git (no PRs) goes to a "patches branch" mode (just a branch, no PR); covered later

## Authentication

Two paths:

### Path 1 — GitHub Personal Access Token (PAT)

User creates a PAT with `repo` scope, sets it in `.env`:

```
GITHUB_TOKEN=ghp_xxx
```

Carve config references it:

```toml
[git]
provider = "github"
token = "${GITHUB_TOKEN}"
default_branch = "main"
```

This is the v0.1 path. Simple, no OAuth flow.

### Path 2 — GitHub App (deferred)

For SaaS, a GitHub App lets users install Carve on specific repos with proper scope. Token rotation, audit logs. Out of scope for v0.1.

## Configuration

`carve/git.toml`:

```toml
[git]
provider = "github"
token = "${GITHUB_TOKEN}"
remote = "origin"             # which git remote to push to
default_branch = "main"       # PR target

[git.pr]
draft = false                 # open PRs as drafts
title_prefix = "[carve]"
labels = ["carve-generated", "auto-pr"]
assign_to = []                # GitHub usernames to auto-assign
request_reviewers = []        # GitHub usernames or teams

[git.branch]
prefix = "carve/"             # all branch names start with this
naming = "carve/<pipeline_name>-<short_id>"  # template
```

`<short_id>` is a 6-character random suffix that disambiguates re-deploys
of the same pipeline. The pipeline name carries the durable identity;
the suffix keeps each deploy's branch unique.

## The deploy flow

`carve deploy <pipeline_name>`:

1. Look up the `Pipeline` row by name. Reject with exit 2 if not found.
2. Resolve `pipeline_dir` (relative path under `pipelines/<name>/`) and
   confirm the files exist on disk. If `main.py` is missing, refuse with
   "no built artifact for this pipeline; run `carve build` first."
3. Check that the working tree is clean **with respect to**
   `pipeline_dir`. Untracked or modified files anywhere else in the repo
   are fine — deploy only touches the pipeline's directory.
4. Determine remote and target branch from `git.toml`.
5. Compute branch name from the template
   (`carve/<pipeline_name>-<short_id>`).
6. Create a new branch from current `default_branch`:
   `git checkout -b <branch>`.
7. Stage the pipeline directory: `git add pipelines/<name>/`.
8. Commit with a structured message (see below).
9. Push the branch: `git push -u origin <branch>`.
10. Open a PR via the GitHub API; apply labels and reviewers.
11. Persist a deploy-run row (`kind="deploy"`, `target_id=pipeline_name`,
    `pipeline_name=<name>`) recording the PR url, the head branch, and
    the plan id that produced the current pipeline state.

If any of steps 6–10 fail, the deploy failed. The local branch may or
may not exist depending on where the failure happened; either way, the
user fixes the underlying issue (auth, network, branch collision) and
re-runs `carve deploy <pipeline_name>`. The branch's `<short_id>` suffix
guarantees the retry won't collide with the previous attempt.

The `Pipeline` row is unchanged by deploy; it tracks dev state, not
deployment state. Tracking deployment history (which deploy opened which
PR, when it merged, etc.) lives on the deploy-run rows.

See M2-01 for the broader plan/deploy lifecycle this slots into.

## Branch naming

`carve/<pipeline_name>-<short_id>` where `<short_id>` is a 6-character
lowercase hex string.

Examples:
- `carve/iowa_liquor_sales-a3f29c`
- `carve/stg_orders-7b412e`

Plan ids and build-run ids are **not** in the branch name — they live
in the commit message and PR body, where they remain useful for
traceability without polluting branch identity. A pipeline that gets
applied four times produces four branches that are easy to scan.

## Commit message

Structured for machine and human readability:

```
[carve] Deploy pipeline iowa_liquor_sales

Pipeline: iowa_liquor_sales
Built from plan: plan_a3f29
Build run:       run_7c11ab
Deploy run:      run_xx88aa

Files in this deployment:
- pipelines/iowa_liquor_sales/main.py
- pipelines/iowa_liquor_sales/requirements.txt

Generated by Carve v0.0.5
```

The `Pipeline:` line is the durable handle. The `Built from plan:` and
`Build run:` lines are traceability data so the UI can link a commit
back to the plan that designed the code and the build that wrote it.
The `Deploy run:` line is the row in the state store that recorded this
particular deployment attempt.

## PR title and description

PR title: `[carve] Deploy pipeline <pipeline_name>` (configurable via
`title_prefix`).

The PR body is a structured markdown document:

```markdown
## Pipeline: iowa_liquor_sales

**Goal:** Daily ingest of the most recent Iowa liquor sales rows.

**Built from plan:** `plan_a3f29`
**Build run:** [run_7c11ab](http://localhost:8787/runs/7c11ab)
**Deploy run:** [run_xx88aa](http://localhost:8787/runs/xx88aa)

## What this deploys

Files included in this PR:
- `pipelines/iowa_liquor_sales/main.py` — extractor + loader for the
  Iowa Socrata endpoint.
- `pipelines/iowa_liquor_sales/requirements.txt` — pinned dependencies.

## Plan summary

- **Source:** Socrata API at `data.iowa.gov/resource/m3tr-qhgy.csv`
  (10 000-row window, ordered `date DESC`).
- **Destination:** `<DATABASE>.<SCHEMA>.IOWA_LIQUOR_SALES`.
- **Strategy:** `MERGE` upsert on `INVOICE_LINE_NO`.
- **Estimated runtime:** ~10 minutes per run.

## Tradeoffs noted at plan time

- Row-by-row MERGE is slow at scale; acceptable at 10k.
- PRIMARY KEY in Snowflake is informational only.

## Generated by Carve

This PR was generated by Carve. Review the changes carefully before merging.

- Pipeline: `iowa_liquor_sales`
- Plan ID: `plan_a3f29`
- Carve version: `0.0.5`
- Generated at: 2026-01-15T14:23:11Z
```

The body is rendered from `templates/pr_body.md.j2` against
`(pipeline, plan, build_run, deploy_run, file_list)`. Plan-summary
content is read out of the saved plan JSON so it stays in sync with
what the user reviewed in `carve plan`.

## Implementation

`src/carve/core/git/github.py`:

```python
class GitHubProvider:
    def __init__(self, config: GitConfig):
        self.config = config
        self.gh = Github(auth=Auth.Token(config.token))

    def open_pr(
        self,
        pipeline: Pipeline,
        plan: Plan,
        build_run: Run,
        deploy_run: Run,
        file_list: list[Path],
        repo_root: Path,
    ) -> str:
        # 1. Detect remote + parse owner/repo
        remote_url = self._git_remote_url(repo_root)
        owner, repo_name = self._parse_github_url(remote_url)

        # 2. Create branch
        branch = self._branch_name(pipeline)
        self._create_branch(repo_root, branch)

        # 3. Stage + commit (only the pipeline directory)
        self._stage_pipeline_dir(repo_root, pipeline.pipeline_dir)
        message = self._commit_message(
            pipeline, plan, build_run, deploy_run, file_list,
        )
        self._commit(repo_root, message)

        # 4. Push
        self._push(repo_root, branch)

        # 5. Open PR
        title = self._pr_title(pipeline)
        body = self._pr_body(
            pipeline, plan, build_run, deploy_run, file_list,
        )
        repo = self.gh.get_repo(f"{owner}/{repo_name}")
        pr = repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base=self.config.default_branch,
            draft=self.config.pr.draft,
        )

        # 6. Apply labels, reviewers
        if self.config.pr.labels:
            pr.add_to_labels(*self.config.pr.labels)
        if self.config.pr.request_reviewers:
            pr.create_review_request(reviewers=self.config.pr.request_reviewers)

        return pr.html_url
```

The `deploy` command in `src/carve/cli/commands/deploy.py` becomes a real
orchestrator entry point: load pipeline, look up the build run via
`Pipeline.current_plan_id` → `Plan.built_run_id`, create the deploy-run
row, instantiate the provider, call `open_pr`, persist the resulting
url on the deploy-run row, print the PR link.

## Local git operations

Use `subprocess` for git commands rather than `pygit2` or `dulwich`:

- The user's git is configured the way they like (signing keys, hooks, etc.)
- Bypassing it would require recreating all that
- Performance is not a concern for the volume of operations Carve needs

```python
def _create_branch(self, repo_root: Path, branch: str) -> None:
    base = self.config.default_branch
    subprocess.check_call(["git", "checkout", base], cwd=repo_root)
    subprocess.check_call(["git", "pull", "--ff-only"], cwd=repo_root)
    subprocess.check_call(["git", "checkout", "-b", branch], cwd=repo_root)

def _stage_pipeline_dir(self, repo_root: Path, pipeline_dir: str) -> None:
    subprocess.check_call(["git", "add", pipeline_dir], cwd=repo_root)

def _commit(self, repo_root: Path, message: str) -> None:
    subprocess.check_call(["git", "commit", "-m", message], cwd=repo_root)

def _push(self, repo_root: Path, branch: str) -> None:
    remote = self.config.remote
    subprocess.check_call(["git", "push", "-u", remote, branch], cwd=repo_root)
```

## Error handling

Common failures and recovery. Deploy is a single transaction from the
user's perspective: success means PR opened, failure means it didn't.

- **Pipeline not found**: exit 2 with "no pipeline named <name>; run `carve pipelines` to list."
- **No built artifact (`main.py` missing)**: exit 2 with "run `carve build <plan_id>` first."
- **Working tree dirty inside `pipeline_dir`**: refuse. Tell the user to commit, stash, or revert their hand-edits — `carve deploy` ships what's on disk and won't silently discard local changes.
- **Branch collision** (random short-id collides with an existing branch — extremely unlikely): regenerate the suffix and retry once; if it still collides, fail with a clear message.
- **No remote configured**: skip PR step, leave the branch local, instruct the user to push manually. Deploy-run is marked failed.
- **Push fails (auth, network)**: deploy failed. Branch may exist locally; the user fixes auth and re-runs `carve deploy <pipeline_name>` (which generates a new branch with a fresh suffix).
- **GitHub API rate limit**: wait + retry with backoff up to 60s.
- **PR API failure (e.g., 422 from a missing label)**: open the PR without the label and warn; this is recoverable and counts as success.

## "no PR" mode

For users without a remote, `carve deploy --no-pr <pipeline_name>` (or
`[git.pr] enabled = false`) skips the push and PR steps. Branch +
commit happen locally; the user pushes and opens the PR by hand. This
is also the v0.1 path for self-hosted GitLab/Bitbucket users.

## Tests

- Branch naming uses pipeline name and a 6-char hex suffix
- Branch suffix is regenerated on collision (mock `git rev-parse --verify` returning success on first call, failure on second)
- Commit message includes pipeline, plan, build-run, deploy-run lines
- PR body renders correctly from a fixture pipeline + plan + run set
- GitHub API errors surface clearly and mark deploy-run failed
- Dirty working tree inside `pipeline_dir` blocks deploy with helpful message
- Dirty working tree outside `pipeline_dir` does not block deploy
- Pipeline not found → exit 2
- Missing `main.py` → exit 2 with "run `carve build` first" message
- `--no-pr` mode commits locally without API calls
- Deploy-run row records branch, head sha, and PR url on success

Use `pygithub`'s test fixtures or VCR-style cassette recordings for API tests.

## Acceptance criteria

- `carve deploy <pipeline_name>` creates a branch named
  `carve/<pipeline_name>-<short_id>`, commits the pipeline's files,
  pushes, and opens a PR against the configured `default_branch`.
- The PR description includes the pipeline goal, plan id, build-run
  link, deploy-run link, file list, and plan summary.
- A failed push or PR-create call marks the deploy-run as failed; the
  user can re-run `carve deploy <pipeline_name>` after fixing the
  underlying issue and a fresh branch (new `<short_id>`) is used.
- `carve deploy` refuses to run if the pipeline doesn't exist or hasn't
  been built (`main.py` missing) — clear exit 2 with actionable text.
- Hand-edits inside `pipelines/<name>/` block deploy until committed or
  reverted; edits elsewhere in the repo do not block deploy.
- `--no-pr` mode works for users without a configured remote.
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover
  the branch logic, commit/PR templates, error paths, and the `--no-pr`
  flag.

## Files this spec produces

New:

- `src/carve/core/git/__init__.py`
- `src/carve/core/git/provider.py` — `GitConfig`, abstract `Provider` base
- `src/carve/core/git/github.py` — `GitHubProvider` implementation
- `src/carve/core/git/branch.py` — branch-name generation, short-id helper, collision retry
- `src/carve/core/git/templates/pr_body.md.j2`
- `src/carve/core/git/templates/commit_message.j2`
- `src/carve/cli/orchestrator/deployer.py` — deploy orchestration (load pipeline + plan + build run, create deploy run, call provider, persist PR url)
- `tests/core/git/__init__.py`
- `tests/core/git/test_branch.py`
- `tests/core/git/test_github.py`
- `tests/cli/orchestrator/test_deployer.py`

Modified:

- `src/carve/cli/commands/deploy.py` — replace the M2 placeholder with a real implementation that delegates to `orchestrator/deployer.py`
- `src/carve/cli/main.py` — wire up the new deploy command (likely already wired; confirm)
- `src/carve/core/state/repository.py` — helpers for creating/finalising deploy-run rows; lookup of the build run that produced a pipeline's current state
- `src/carve/core/state/models.py` — extend `Run` with optional fields recording the PR url and head branch when `kind="deploy"` (or store on the run row's `error_message`/a new JSON column — engineer's call, but acceptance requires the PR url to be retrievable from the deploy-run)
- `pyproject.toml` — add `PyGithub` runtime dependency
- `README.md` — document the deploy flow
- `CHANGELOG.md` — entry under `## [Unreleased]`

## What this enables

- The full `plan` → `build` → `run` (dev) → `deploy` (prod-PR) loop is the demo flow for v0.0.5
- Generated code is reviewable before it lands on `main`
- CI runs on the PR catches issues the agent missed
- Deploy is idempotent at the pipeline level: re-running it ships the current on-disk state of the pipeline, regardless of how many builds happened in between
- Future GitLab/Bitbucket support is just another `Provider` subclass behind the same `deployer.py` orchestration
