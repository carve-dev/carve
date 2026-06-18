# Control-plane layout: `carve.toml` config + `[components.<name>]` topology; convention-based simple mode

> **Revised for the control-plane model** ([../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md), concrete shapes in [../_strategy/control-plane-reference-model.md](../_strategy/control-plane-reference-model.md)). This is the **foundation** spec: it reframes `carve.toml` as the *control-plane config* (project metadata + connections + **component references**), generalizes the old singular `[dbt]` / `[dlt]` topology blocks into N named, typed `[components.<name>]` blocks (each with a `type`, a `mode`, mode-specific `url`/`branch`/`path`, and an optional per-component `ref` pin), and defines the convention-based **simple mode** that hides all of this until a component is split out. `[el]` is no longer special — EL artifacts are just `type = "dlt"` components, symmetric with dbt.

> Locks the directory layout on disk and the `carve.toml` control-plane schema that records which components the control plane references and where each one resolves, per [PRD §6.2](../PRD.md), [ARCHITECTURE §3](../ARCHITECTURE.md), [ARCHITECTURE §10.1](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 3](../PROJECT_PLAN.md). Carries forward content from the archived [P1.1-01 flat-layout draft](../_archive/pillar-1.1-flat-layout/01-flat-layout.md), revised to reflect that `el/<name>/` contents are dlt pipeline files rather than bespoke Python EL scripts.

## Status

- **Status:** Drafting
- **Depends on:** None directly (purely structural)
- **Blocks:** [dlt-engineer](./dlt-engineer.md) (the agent writes files into this layout), [init](./init.md) (init creates this layout), [runtime](./runtime.md) (workers resolve paths via this layout), [pipelines](./pipelines.md) (`dlt`/`dbt` step types resolve targets via this layout)

## Goal

Define and lock:

1. The canonical directory layout on disk for a single-repo (simple-mode) Carve control plane
2. The `carve.toml` **control-plane** schema: project metadata, connections, and the `[components.<name>]` blocks that reference each typed component (`type` = dlt|dbt), declare its topology (`mode` = same-repo|separate-local|separate-remote with mode-specific `url`/`branch`/`path`), and optionally pin it (`ref`)
3. The convention-based **simple-mode discovery** — each `el/<name>/` dir is a `dlt` component named `<name>`; the detected dbt project is a `dbt` component — so no `[components.*]` blocks are required until a component is split out (progressive disclosure)
4. The name-based **component resolution** logic that runtime code uses to find a component's code at invocation time, plus `carve components show` to inspect the resolved references
5. The provenance convention for Carve-generated dlt files (so users and agents can tell what came from where)
6. The workspace cache for `separate-remote` components (`.carve/workspaces/<name>/` with git sync semantics) — generalized to any separate-remote component (dlt or dbt), not just dbt

After this spec lands, every other v0.1 spec assumes this control-plane config + layout. Init scaffolds it (spec 05), the EL agent writes into the resolved component (spec 04), the runtime resolves component names through it (specs 07, 08). Pipeline steps reference components **by name** (`component = "<name>"`) and the graduation/inspection commands (`carve component`, `carve components show`, `carve schedule reseed`) are specified in [pipelines](./pipelines.md), which *consumes* the resolver this spec defines.

## Out of scope

- The contents of generated dlt files (sources, resources, `pipeline.run()` calls) — that's [dlt-engineer](./dlt-engineer.md).
- The `pipelines/<name>.toml` schema itself (steps, `depends_on`, failure modes, the `[seed_schedule]` block, the `component = "<name>"` step field) — that's [pipelines](./pipelines.md). This spec defines the `[components.<name>]` blocks in `carve.toml` and the name→code resolver; spec 08 *consumes* that resolver from the pipeline steps.
- **Component graduation + the `carve component` / `carve components show` / `carve schedule reseed` command implementations** — those CLI commands ship in [pipelines](./pipelines.md). This spec defines the `[components.<name>]` block they write/read, the resolution they rely on, and the *behavior* of `carve components show` (what resolved references it must surface); spec 08 wires up the Typer commands.
- Per-pipeline memory sidecars (`pipelines/<name>.md`, `el/<name>/NOTES.md`) — that's [memory](./memory.md). This spec carves out the *locations* but doesn't ship the read/write code.
- The `carve init` UX that creates the layout — that's [init](./init.md). This spec defines what gets created; init wires up the user-facing flow.
- **Deploy behavior** (`carve deploy`, per-component promotion, the cross-repo linked-PR flow) — unchanged here and pending the Wave 2 deploy revision of [deploy](./deploy.md). Where this spec touches sync-before-deploy and the separate-remote authoring-via-PR guardrail, that behavior is left as-is and noted as pending the Wave 2 deploy revision.

## Behavior

### Canonical directory layout

The single-repo **simple-mode** shape — the delightful default, where the control-plane config and all components live in one working tree — looks like this. By convention, no `[components.*]` blocks are written (see *Simple-mode discovery* below); they materialize only when a component is split out to a separate repo.

```
my-carve-control-plane/
├── carve.toml                       # control-plane config: project metadata; default target; connections; [components.<name>] blocks (omitted in simple mode)
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
│       └── *.md                     # agent definitions (markdown + frontmatter, spec 16); optional overrides for built-ins
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
    │   └── <name>/                  # cloned separate-remote component (dlt or dbt), per its [components.<name>] block
    └── token                        # local API token (mode 0600)
```

Separate-repo and separate-remote modes shift the dbt and/or dlt sections out of the project root; details in the *Path resolution* section below.

### `carve.toml` schema additions

```toml
# Existing root-level fields are unchanged: project name, default_target, connections, etc.

# Each component the control plane references is a named, typed block. `type` is
# "dlt" or "dbt"; `mode` declares its topology. Omit all [components.*] blocks for
# the convention-based simple mode (see "Simple-mode discovery" below).

[components.analytics]
type = "dbt"
mode = "separate-remote"    # WHERE the code lives (repo topology): same-repo | separate-local | separate-remote
url  = "git@github.com:myorg/analytics.git"
ref  = "9f3a1c7"            # optional pin (commit or tag); see ref-vs-branch precedence below
# branch = "main"           # track a branch's HEAD instead of pinning
# dbt_backend = "snowflake-native"   # HOW dbt runs — an ORTHOGONAL axis to `mode`: local | snowflake-native | dbt-cloud | remote
#                                    # local-only: dbt_engine/dbt_version (pinned on first resolve), dbt_env, worker_label.
#                                    # Full per-backend config + the engine default (Fusion/dbt-core) in capabilities/dbt-execution.md.

[components.stripe_charges]
type = "dlt"
mode = "separate-local"
path = "/path/to/ingest-stripe"   # required when mode == "separate-local"
```

Three key properties:

1. Components are **named and typed**. A pipeline step references one by name (`component = "stripe_charges"`, spec 08); `type` (`dlt`|`dbt`) tells the locator how to resolve and run it.
2. Components are **independent**. Any mix of types/modes is allowed (a `separate-remote` dbt component alongside a `same-repo` dlt component, etc.).
3. The blocks are **optional**. Omitting all `[components.*]` blocks is the convention-based **simple mode** (see below). Blocks materialize only when a component is split out (`carve component …`, spec 08).

**`ref` vs `branch` precedence** (per the control-plane reference model): for a `separate-remote` component, `ref` (a commit SHA or tag) is a **pin** — the locator checks out exactly that revision. If `ref` is unset but `branch` is set, the component **tracks that branch's HEAD** (hard-synced before use). If neither is set, it tracks the remote's default branch HEAD. `ref` always wins over `branch` when both are present. Convention (simple-mode) components are never pinned.

### Simple-mode discovery

When `carve.toml` has no `[components.*]` blocks (the default), components are discovered by convention: each `el/<name>/` directory is a `dlt` component named `<name>`; the single detected dbt project (`<root>/dbt_project.yml`, or one level down) is a `dbt` component. No blocks, no pins — the machinery stays hidden until a component is split out. `carve components show` (spec 08) lists the convention-discovered components so the implicit set is always inspectable.

### Path resolution

`src/carve/core/config/paths.py` exposes a `ProjectPaths` dataclass for the fixed control-plane paths:

```python
@dataclass(frozen=True)
class ProjectPaths:
    root: Path                    # control-plane root (where carve.toml lives)
    carve_dir: Path               # <root>/carve/
    el_dir: Path                  # <root>/el/
    pipelines_dir: Path           # <root>/pipelines/
    scratch_dir: Path             # <root>/.carve/
    dlt_config_dir: Path          # <root>/.dlt/
```

Component code is resolved **by name** through `src/carve/integrations/component_locator.py`:

```python
def resolve_component(name: str, *, components, paths: ProjectPaths) -> ResolvedComponent: ...
# ResolvedComponent: (name, type ["dlt"|"dbt"], code_path: Path, ref: str | None)
```

Resolution rules, per the component's `mode` (or convention in simple mode):

- **`same-repo`, `type = "dlt"`** (or a convention `el/<name>/` dir): the component's code path is `<root>/el/<name>/`; the dlt project root is `<root>` (where `.dlt/` lives).
- **`same-repo`, `type = "dbt"`** (or the convention-detected dbt project): search `<root>/dbt_project.yml`, then `<root>/*/dbt_project.yml` (one level down). Exactly one → that's the path. Zero → brownfield-dbt absent (init handles greenfield via `--with-dbt`). Multiple → error with the list; the user pins one via a `[components.<name>]` block (`mode = "separate-local"`, `path = …`).
- **`separate-local`**: the recorded `path` is the component's code path. Must exist; error at startup if not.
- **`separate-remote`**: workspace cache at `<root>/.carve/workspaces/<derived-name>/` (derived name = `slugify(url) + "-" + ref-or-branch`), synced via the workspace cache module below and checked out at the component's pinned `ref` (or the branch HEAD if unpinned, per the precedence rule above).

Every runtime call site (`dlt`/`dbt` step executors, the EL agent's file-write target, the dbt agent's in v0.2, manifest reader, sources.yml reader) resolves component code through `resolve_component` — no path math elsewhere. `carve components show` (spec 08) surfaces the resolved set.

### Workspace cache for `separate-remote` mode

`src/carve/integrations/workspace_cache.py` exposes:

```python
def sync_workspace(name: str, url: str, branch: str, paths: ProjectPaths) -> Path: ...
def is_dirty(workspace_path: Path) -> bool: ...
def reject_if_dirty(workspace_path: Path) -> None: ...
```

Semantics:

- **`sync_workspace`** is idempotent. On first call: `git clone <url> <workspace_path> --branch <branch>`. On subsequent calls: `git fetch origin && git checkout <branch> && git reset --hard origin/<branch>` (configurable; default is hard sync, opt out via `[components.<name>] sync_mode = "soft"` for users who want to `git pull` instead).
- **`is_dirty`** returns true if the workspace has uncommitted changes or untracked files (excluding the workspace's own .gitignored entries).
- **`reject_if_dirty`** called before every sync; if the workspace is dirty, raises with a friendly message pointing the user at the workspace path and telling them to either commit/discard their changes or take the workspace out of cache management.

A `workspaces` table in the state store records, per cached repo: name, url, branch, last_synced_commit, last_synced_at, status (`clean`, `dirty`, `unreachable`). The runtime queries this for diagnostics in the static UI.

Sync triggers:

- On `carve serve` startup (best-effort; failure to sync emits a warning but doesn't block startup if a valid clone already exists)
- Before each pipeline run (before the worker invokes the step executor; configurable via `[components.<name>] sync_before_run = false` for offline operation)
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

- The header is parsed by `src/carve/integrations/dlt/provenance.py` and made available as structured metadata to other parts of the system (e.g., spec 19's `dlt_schema` reader uses it for the component's `destination` binding during lineage investigation; and any future provenance view).
- Files without the header are treated as user-authored (orchestration-only artifacts per PRD §6.2 mode 2). The agent never modifies them.
- Re-generation preserves user edits below the header — the agent diffs against the previous build's content and either merges cleanly or surfaces the conflict in the plan for user review.
- For curated-library copies (per [ARCHITECTURE §5.8](../ARCHITECTURE.md)), the header records the library commit so users can opt-in to refresh.

### File-write guardrail extensions

The file-write guardrail at the skill layer (referenced in [ARCHITECTURE §12.5](../ARCHITECTURE.md)) extends to:

- Allow writes to `<root>/el/<name>/**` (for EL agent output)
- Allow writes to `<root>/pipelines/**` (for multi-step composition)
- Allow writes to `<root>/.dlt/config.toml.template` and `<root>/.dlt/secrets.toml.template` (templates only — never the real `.dlt/secrets.toml` which contains live credentials)
- Allow writes to `<root>/carve/conventions.md` (refresh from inference; spec 06)
- Allow writes within the resolved dbt project path under `models/`, `tests/`, `sources.yml`, and similar dbt-managed directories — *only when the dbt component's mode allows authoring (same-repo and separate-local; separate-remote produces a PR against the remote rather than a local file write)*
- Reject writes anywhere else with a `WriteOutsideAllowedPaths` skill error

### Init touchpoint

`carve init` (spec 05) consults any `[components.*]` blocks if they exist in a partially-populated `carve.toml` (e.g., the user manually set up the file before running init). Otherwise, init scaffolds the convention-based simple mode and writes **no** `[components.*]` blocks (they're added later only when a component is split out).

When `--dbt-url <git_url>` or `--dlt-url <git_url>` are passed, init writes a `[components.<name>]` block (`type` = dbt or dlt) with `mode = "separate-remote"` and triggers an initial workspace sync. When `--dbt-path` or `--dlt-path` are passed, init writes the block with `mode = "separate-local"`. Otherwise, init leaves the components implicit (simple mode) — it scans for `dbt_project.yml` and `el/`-shaped trees and confirms the convention rather than writing blocks.

### Migration from M1.1 / pillar-1.1 drafts

A small number of working-skeleton + M1.1 users have shipped projects with the `targets/<name>/el/<artifact>/` layout (from the original pillar-1 design). For those users:

- `carve init --migrate-from-targets` walks the project, moves `targets/<target>/el/<artifact>/` to `el/<artifact>/`, and records target-specific configuration in `.dlt/config.toml`'s per-target sections instead
- The migration is one-shot and not undoable
- Pre-migration state is committed first via a git commit named "Pre-Carve-v0.1-layout-migration" so the user can revert

## Tests

- **Unit:** `carve.toml` parses with various `[components.<name>]` block shapes (omitted → simple-mode convention, same-repo, separate-local with path, separate-remote with url + ref/branch); invalid shapes raise structured errors; `ref` pins and `branch`-tracking resolve per the precedence rule
- **Unit:** `ProjectPaths` resolves same-repo / separate-local / separate-remote correctly; raises clean errors on missing paths or unreachable URLs
- **Unit:** `slugify(url) + "-" + branch` is stable and collision-safe for representative URLs
- **Unit:** provenance header parses cleanly; missing fields surface as `None` rather than parse failures
- **Integration:** `sync_workspace` clones a remote git repo, syncs on second call, hard-resets on a force-push, detects dirty state
- **Integration:** `reject_if_dirty` blocks sync when local modifications exist, with the expected error message
- **Integration:** a fresh `carve init` in greenfield writes **no** `[components.*]` blocks (convention-based simple mode), the layout matches the canonical tree, and component discovery finds the `el/<name>/` dlt components + the detected dbt project
- **Integration:** `carve init --dbt-url <test-git-url>` writes a `[components.<name>]` (`type = "dbt"`) block with `mode = "separate-remote"` and triggers an initial workspace clone
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

- **`sync_mode` per backend (hard vs soft).** *Implementation default.* Default to hard sync (`git fetch && git reset --hard`); this is the safest default for "the workspace should match the remote." Soft sync (`git pull`) preserves any uncommitted local changes but is footgun-prone. Users who want pull semantics can opt in via `[components.<name>] sync_mode = "soft"` in `carve.toml`.
- **Workspace cache eviction policy.** *Implementation default.* No automatic eviction in v0.1; manual `carve workspaces clear <name>` if a user wants to force a fresh clone. With cache caps configurable in `runtime.toml` if it becomes an issue.
- **Behavior when `dbt_project.yml` is found more than one level down (e.g., `<root>/some/deep/path/dbt_project.yml`).** *Implementation default.* Discovery only looks at root and one level down by default. Deeper paths require an explicit `[components.<name>]` block (`type = "dbt"`, `mode = "separate-local"`, `path = …`). The shallow default avoids false positives in monorepos that have many `dbt_project.yml` files (e.g., one per service).
- **Should the workspace cache support sparse-checkout for large remote dbt repos?** *Implementation default.* No in v0.1. dbt projects fit in a typical clone; sparse-checkout adds complexity for a problem we don't have yet. Revisit if a user hits it.
