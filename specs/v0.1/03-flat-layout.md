# v0.1-03 — Flat `el/<name>/` layout for dlt artifacts; per-backend repo topology

> Locks the directory layout of a Carve project and the `carve.toml` schema that records where the dbt and dlt projects live, per [PRD §6.2](../PRD.md), [ARCHITECTURE §3](../ARCHITECTURE.md), [ARCHITECTURE §10.1](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 3](../PROJECT_PLAN.md). Carries forward content from the archived [P1.1-01 flat-layout draft](../_archive/pillar-1.1-flat-layout/01-flat-layout.md), revised to reflect that `el/<name>/` contents are dlt pipeline files rather than bespoke Python EL scripts.

## Status

- **Status:** Drafting
- **Depends on:** None directly (purely structural)
- **Blocks:** [v0.1-04 el-agent-dlt](./04-el-agent-dlt.md) (the agent writes files into this layout), [v0.1-05 init-rewrite](./05-init-rewrite.md) (init creates this layout), [v0.1-07 runtime](./07-runtime.md) (workers resolve paths via this layout), [v0.1-08 multi-step-pipeline](./08-multi-step-pipeline.md) (`dlt`/`dbt` step types resolve targets via this layout)

## Goal

Define and lock:

1. The canonical directory layout of a Carve project on disk
2. The `carve.toml` schema for declaring the dbt and dlt repo topologies (same-repo, separate-local, separate-remote)
3. The path-resolution logic that runtime code uses to find dbt and dlt artifacts at invocation time
4. The provenance convention for Carve-generated dlt files (so users and agents can tell what came from where)
5. The workspace cache for `separate-remote` mode (`.carve/workspaces/<name>/` with git sync semantics)

After this spec lands, every other v0.1 spec assumes this layout. Init scaffolds it (spec 05), the EL agent writes into it (spec 04), the runtime resolves through it (specs 07, 08).

## Out of scope

- The contents of generated dlt files (sources, resources, `pipeline.run()` calls) — that's [v0.1-04 el-agent-dlt](./04-el-agent-dlt.md).
- The `pipelines/<name>.toml` schema itself (steps, depends_on, failure modes, schedule blocks) — that's [v0.1-08 multi-step-pipeline](./08-multi-step-pipeline.md).
- Per-pipeline memory sidecars (`pipelines/<name>.md`, `el/<name>/NOTES.md`) — that's [v0.1-06 project-memory](./06-project-memory.md). This spec carves out the *locations* but doesn't ship the read/write code.
- The `carve init` UX that creates the layout — that's [v0.1-05 init-rewrite](./05-init-rewrite.md). This spec defines what gets created; init wires up the user-facing flow.

## Files this spec produces

```
src/carve/core/config/project.py             # MODIFY (or NEW) — carve.toml schema + parsing for [dbt] and [dlt] blocks
src/carve/core/config/paths.py               # NEW — canonical path resolution (project root, el dir, pipelines dir, dbt project path, dlt project path)
src/carve/integrations/dbt/locator.py        # NEW — resolves dbt project path per [dbt] block (same-repo / separate-local / separate-remote)
src/carve/integrations/dlt/locator.py        # NEW — resolves dlt project path per [dlt] block
src/carve/integrations/workspace_cache.py    # NEW — clone/sync helper for separate-remote mode
src/carve/core/state/models.py               # MODIFY — workspaces table (tracks last-synced commit per remote-cached repo)
migrations/versions/0007_workspaces.py       # NEW — Alembic migration adding the `workspaces` table
src/carve/integrations/dlt/provenance.py     # NEW — read/write the provenance header in generated dlt files
tests/unit/test_paths.py                     # NEW — path resolution unit tests
tests/integration/test_workspace_cache.py    # NEW — clone, sync, conflict-on-local-modification
docs/project-layout.md                       # NEW — user-facing reference for the layout and the carve.toml [dbt]/[dlt] blocks
```

## Behavior

### Canonical directory layout

A Carve project (same-repo mode for both backends; the most common shape) looks like:

```
my-carve-project/
├── carve.toml                       # project metadata; default target; [dbt]/[dlt] topology blocks
├── docker-compose.yml               # bundled Postgres (from spec 02), optional with --external-postgres
├── .env                             # gitignored; credentials, DATABASE_URL
├── .env.example                     # committed template
├── .gitignore                       # carve-specific entries
├── .dlt/                            # dlt's own config dir (project-root scope, dlt's convention)
│   ├── config.toml                  # destination configs (per-target sections)
│   └── secrets.toml                 # credentials (env-var-interpolated)
├── carve/                           # carve's own project state
│   ├── connections.toml             # target definitions
│   ├── runtime.toml                 # scheduler/worker tuning
│   ├── conventions.md               # inferred (spec 06)
│   ├── standards.md                 # user-authored (spec 06)
│   ├── decisions.md                 # append-only (spec 06)
│   └── agents/
│       └── *.toml                   # agent definitions; optional, agent overrides for built-ins
├── el/                              # dlt artifacts; one directory per pipeline
│   ├── stripe_charges/
│   │   ├── __init__.py              # dlt source + resource definitions
│   │   ├── requirements.txt         # pinned deps (dlt[snowflake] + SaaS client)
│   │   └── NOTES.md                 # optional per-EL memory sidecar (spec 06)
│   └── salesforce_accounts/
│       └── ...
├── pipelines/                       # multi-step pipeline composition (spec 08)
│   ├── stripe.toml                  # one TOML per pipeline
│   └── stripe.md                    # optional per-pipeline memory sidecar (spec 06)
├── dbt_project.yml                  # dbt project file (same-repo mode); may live one level down
├── models/                          # dbt models
│   ├── staging/
│   └── marts/
├── tests/                           # dbt tests (user-authored)
├── sources.yml                      # dbt sources
└── .carve/                          # runtime scratch + cache (gitignored)
    ├── plans/                       # plan JSON files
    ├── asks/                        # ask JSON files (spec 12)
    ├── workspaces/                  # separate-remote workspace cache (this spec)
    │   └── <name>/                  # cloned dbt or dlt repo per [dbt]/[dlt] block
    └── token                        # local API token (mode 0600)
```

Separate-repo and separate-remote modes shift the dbt and/or dlt sections out of the project root; details in the *Path resolution* section below.

### `carve.toml` schema additions

```toml
# Existing root-level fields are unchanged: project name, default_target, etc.

[dbt]
# Repo topology for dbt. Default is "same-repo".
mode = "same-repo"          # one of: "same-repo", "separate-local", "separate-remote"

# Required when mode == "separate-local":
# path = "/path/to/dbt-project"

# Required when mode == "separate-remote":
# url = "git@github.com:myorg/dbt-project.git"
# branch = "main"           # defaults to "main"

[dlt]
# Repo topology for dlt. Default is "same-repo".
mode = "same-repo"

# Same path/url/branch shape as [dbt] when mode != "same-repo".
```

Two key properties:

1. The two blocks are **independent**. A user can run `[dbt] mode = "separate-remote"` with `[dlt] mode = "same-repo"`, or any other combination.
2. The blocks are **optional**. Omitting both is equivalent to declaring both `mode = "same-repo"` — for greenfield projects with no existing dbt or dlt setup, init can scaffold without writing these blocks.

### Path resolution

`src/carve/core/config/paths.py` exposes a `ProjectPaths` dataclass with:

```python
@dataclass(frozen=True)
class ProjectPaths:
    root: Path                    # project directory (where carve.toml lives)
    carve_dir: Path               # <root>/carve/
    el_dir: Path                  # <root>/el/
    pipelines_dir: Path           # <root>/pipelines/
    scratch_dir: Path             # <root>/.carve/
    dlt_config_dir: Path          # <root>/.dlt/
    dbt_project_path: Path        # resolved per [dbt] block; see below
    dlt_project_path: Path        # resolved per [dlt] block; see below
```

Resolution rules:

- **`mode = "same-repo"`** for dbt: search `<root>/dbt_project.yml`, then `<root>/*/dbt_project.yml` (one level down). If exactly one found, that's the dbt project path. If zero, brownfield-dbt is absent (init handles greenfield via `--with-dbt`). If multiple, error with the list; user must pick one via `[dbt] path = ...` and `mode = "separate-local"`.
- **`mode = "same-repo"`** for dlt: the canonical dlt project path is `<root>` itself (where `.dlt/` lives), with `el/` as the discovery root for individual pipelines.
- **`mode = "separate-local"`**: the recorded `path` is the project path. Must exist; error at startup if not.
- **`mode = "separate-remote"`**: workspace cache at `<root>/.carve/workspaces/<derived-name>/`. The derived name is `slugify(url) + "-" + branch`. Sync happens via the workspace cache module described below.

Every runtime call site (`dlt` step executor, `dbt` step executor, EL agent's file-write target, dbt agent's file-write target in v0.2, manifest reader, sources.yml reader) goes through `ProjectPaths` — no path math elsewhere.

### Workspace cache for `separate-remote` mode

`src/carve/integrations/workspace_cache.py` exposes:

```python
def sync_workspace(name: str, url: str, branch: str, paths: ProjectPaths) -> Path: ...
def is_dirty(workspace_path: Path) -> bool: ...
def reject_if_dirty(workspace_path: Path) -> None: ...
```

Semantics:

- **`sync_workspace`** is idempotent. On first call: `git clone <url> <workspace_path> --branch <branch>`. On subsequent calls: `git fetch origin && git checkout <branch> && git reset --hard origin/<branch>` (configurable; default is hard sync, opt out via `[<backend>] sync_mode = "soft"` for users who want to `git pull` instead).
- **`is_dirty`** returns true if the workspace has uncommitted changes or untracked files (excluding the workspace's own .gitignored entries).
- **`reject_if_dirty`** called before every sync; if the workspace is dirty, raises with a friendly message pointing the user at the workspace path and telling them to either commit/discard their changes or take the workspace out of cache management.

A `workspaces` table in the state store records, per cached repo: name, url, branch, last_synced_commit, last_synced_at, status (`clean`, `dirty`, `unreachable`). The runtime queries this for diagnostics in the static UI.

Sync triggers:

- On `carve serve` startup (best-effort; failure to sync emits a warning but doesn't block startup if a valid clone already exists)
- Before each pipeline run (before the worker invokes the step executor; configurable via `[<backend>] sync_before_run = false` for offline operation)
- Before `carve deploy` (always; failure to sync blocks the deploy)

### Provenance header convention

Files generated by Carve's EL agent include a header comment block. Concrete shape:

```python
# ─────────────────────────────────────────────────────────────────────
# Generated by Carve from carve/sources/stripe at commit abc1234.
# Customized for destination "snowflake.raw_stripe".
# Generated: 2026-05-19T14:23:01Z
# Plan: plan_a1b2c3d4
# Build: build_e5f6g7h8
# Do not edit this header. Edits below the header are preserved on regenerate.
# ─────────────────────────────────────────────────────────────────────
```

- The header is parsed by `src/carve/integrations/dlt/provenance.py` and made available as structured metadata to other parts of the system (e.g., the lineage graph, the static UI's "where did this come from?" view).
- Files without the header are treated as user-authored (orchestration-only artifacts per PRD §6.2 mode 2). The agent never modifies them.
- Re-generation preserves user edits below the header — the agent diffs against the previous build's content and either merges cleanly or surfaces the conflict in the plan for user review.
- For curated-library copies (per [ARCHITECTURE §5.8](../ARCHITECTURE.md)), the header records the library commit so users can opt-in to refresh.

### File-write guardrail extensions

The file-write guardrail at the skill layer (referenced in [ARCHITECTURE §12.5](../ARCHITECTURE.md)) extends to:

- Allow writes to `<root>/el/<name>/**` (for EL agent output)
- Allow writes to `<root>/pipelines/**` (for multi-step composition)
- Allow writes to `<root>/.dlt/config.toml.template` and `<root>/.dlt/secrets.toml.template` (templates only — never the real `.dlt/secrets.toml` which contains live credentials)
- Allow writes to `<root>/carve/conventions.md` (refresh from inference; spec 06)
- Allow writes within the resolved dbt project path under `models/`, `tests/`, `sources.yml`, and similar dbt-managed directories — *only when the [dbt] mode allows authoring (same-repo and separate-local; separate-remote produces a PR against the remote rather than a local file write)*
- Reject writes anywhere else with a `WriteOutsideAllowedPaths` skill error

### Init touchpoint

`carve init` (spec 05) consults `[dbt]` and `[dlt]` blocks if they exist in a partially-populated `carve.toml` (e.g., user manually set up the file before running init). Otherwise, init prompts for topology and writes the blocks.

When `--dbt-url <git_url>` or `--dlt-url <git_url>` are passed, init sets `mode = "separate-remote"` and triggers an initial workspace sync. When `--dbt-path` or `--dlt-path` are passed, init sets `mode = "separate-local"`. Otherwise, init scans for `dbt_project.yml` and `el/`-shaped trees and either confirms `same-repo` or asks the user.

### Migration from M1.1 / pillar-1.1 drafts

A small number of working-skeleton + M1.1 users have shipped projects with the `targets/<name>/el/<artifact>/` layout (from the original pillar-1 design). For those users:

- `carve init --migrate-from-targets` walks the project, moves `targets/<target>/el/<artifact>/` to `el/<artifact>/`, and records target-specific configuration in `.dlt/config.toml`'s per-target sections instead
- The recovery agent (spec deferred post-v0.1) is not part of v0.1; the migration is one-shot and not undoable
- Pre-migration state is committed first via a git commit named "Pre-Carve-v0.1-layout-migration" so the user can revert

## Tests

- **Unit:** `carve.toml` parses with various `[dbt]`/`[dlt]` block shapes (omitted, same-repo, separate-local with path, separate-remote with url+branch); invalid shapes raise structured errors
- **Unit:** `ProjectPaths` resolves same-repo / separate-local / separate-remote correctly; raises clean errors on missing paths or unreachable URLs
- **Unit:** `slugify(url) + "-" + branch` is stable and collision-safe for representative URLs
- **Unit:** provenance header parses cleanly; missing fields surface as `None` rather than parse failures
- **Integration:** `sync_workspace` clones a remote git repo, syncs on second call, hard-resets on a force-push, detects dirty state
- **Integration:** `reject_if_dirty` blocks sync when local modifications exist, with the expected error message
- **Integration:** a fresh `carve init` in greenfield writes `[dbt]` and `[dlt]` blocks with `mode = "same-repo"` and the layout matches the canonical tree
- **Integration:** `carve init --dbt-url <test-git-url>` records `mode = "separate-remote"` and triggers an initial workspace clone
- **Integration:** `carve init --migrate-from-targets` against a synthetic `targets/dev/el/iowa_liquor/` tree produces the expected post-migration layout

## Acceptance

- A v0.1 install (from `carve init`) in greenfield or brownfield-same-repo produces the canonical directory layout
- The three topologies (same-repo, separate-local, separate-remote) are each runnable end-to-end for both dbt and dlt independently (so all nine pairwise combinations work)
- The workspace cache correctly clones, syncs, and detects dirty state for separate-remote
- Carve-generated dlt files carry the provenance header; the header is machine-readable
- File writes from agents are restricted to the allowed paths; a guardrail violation produces a clean error and aborts the build
- Existing M1.1-era projects with `targets/<name>/` layouts can be migrated via `carve init --migrate-from-targets` to the new flat layout without data loss

## Design notes

- **Why flat `el/<name>/` instead of `targets/<target>/el/<name>/`?** The old per-target layout (from pre-positioning Pillar 1) presumed that each target had its own copy of every EL artifact. That doesn't match how dlt works — dlt's destinations are config, not separate code paths. One artifact serves all targets; the active target is selected at runtime via env vars and `.dlt/config.toml` sections. The flat layout reflects this.
- **Why a project-root `.dlt/` rather than per-pipeline?** dlt's own convention. `dlt pipeline run` walks up looking for `.dlt/`; running with everything at the project root means the user's `.dlt/config.toml` lists all destinations and pipelines pick what they need by name. Per-pipeline `.dlt/` is supported by dlt but adds duplication for no benefit when one project has multiple pipelines hitting the same destination.
- **Why `mode = "same-repo" / "separate-local" / "separate-remote"` as three discrete strings?** It's an enum-shaped decision and trying to infer mode from the presence/absence of `path`/`url` makes for surprising behavior. Explicit is friendlier for users and easier to validate.
- **Why workspace cache in `.carve/workspaces/` rather than checking out next to the project?** Keeping it under `.carve/` (gitignored) means the workspace is unambiguously Carve-managed; users don't accidentally commit a clone of their dbt repo to their Carve repo. Also keeps the canonical layout (above) clean of caches.
- **Why a provenance header rather than a registry file?** A registry file (`carve/generated.toml` listing every generated file) is brittle: file moves, renames, and partial regenerations break it. A header in the file is local, survives moves, and is the dlt-tool-friendly convention (`dlt init` puts a similar comment block in its generated sources). The cost is users see a multi-line comment at the top of generated files; we judge that acceptable.

## Open questions

- **`sync_mode` per backend (hard vs soft).** *Implementation default.* Default to hard sync (`git fetch && git reset --hard`); this is the safest default for "the workspace should match the remote." Soft sync (`git pull`) preserves any uncommitted local changes but is footgun-prone. Users who want pull semantics can opt in via `[<backend>] sync_mode = "soft"` in `carve.toml`.
- **Workspace cache eviction policy.** *Implementation default.* No automatic eviction in v0.1; manual `carve workspaces clear <name>` if a user wants to force a fresh clone. With cache caps configurable in `runtime.toml` if it becomes an issue.
- **Behavior when `dbt_project.yml` is found more than one level down (e.g., `<root>/some/deep/path/dbt_project.yml`).** *Implementation default.* Discovery only looks at root and one level down by default. Deeper paths require explicit `[dbt] path = ...`. The shallow default avoids false positives in monorepos that have many `dbt_project.yml` files (e.g., one per service).
- **Should the workspace cache support sparse-checkout for large remote dbt repos?** *Implementation default.* No in v0.1. dbt projects fit in a typical clone; sparse-checkout adds complexity for a problem we don't have yet. Revisit if a user hits it.
