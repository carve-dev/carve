# P1-01 — Target system

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1 day
**Dependencies:** M1-02 (config)
**Lineage:** Net-new. The per-target folder model was synthesized during this session's design discussion; no direct M1/M1.1/M2 ancestor. Foundation that the rest of Pillar 1 evolves on top of.

## Purpose

Establish the per-target folder convention that every Pillar 1+ artifact lives in, the `--target` flag pattern that every Carve verb honors, the `default_target` config in `carve.toml`, and the `carve target` subcommand family for managing target lifecycle. The rest of Pillar 1 (and every later pillar) reads from / writes to these conventions; no other spec re-defines them.

## Folder convention

A target is an environment (dev, staging, prod, qa, eu_prod, etc.) plus everything Carve needs to interact with it. The convention:

```
project-root/
├── carve.toml                          # default_target = "dev"
├── targets/
│   └── <name>/
│       ├── .env                        # gitignored; secrets resolved by P1-04
│       ├── .env.example                # tracked; template
│       ├── connections.toml            # tracked; connection structure for this target
│       ├── el/                         # Pillar 1 artifacts (created in this spec)
│       │   └── <artifact_name>/        # populated by `carve build`
│       │       ├── main.py
│       │       └── requirements.txt
│       └── snowflake/                  # Pillar 1 generated DDL (created lazily by P1-07)
│           └── <artifact_name>.sql
└── .gitignore                          # includes `targets/*/.env`
```

**Reserved subdirectory names** that later pillars own. They are *not* created by this spec — each pillar creates its own subdirectory the first time it lands an artifact:

- `targets/<name>/pipelines/` — Pillar 3
- `targets/<name>/schedules/` — Pillar 4

Empty placeholder directories are explicitly avoided. A target's tree mirrors what's actually deployed to that target, so users can reason about state by reading the filesystem.

## `default_target` and the `--target` flag

### Config

`carve.toml`:

```toml
[project]
name = "my_carve_project"
version = "0.0.1"
default_target = "dev"
```

`default_target` is a string referencing a target directory under `targets/`. Set to `"dev"` by `carve init` (P1-03). Users can change it directly in `carve.toml` or via `carve target rename` when renaming the default.

### Resolution

Active target is resolved at command dispatch time, in this order (first hit wins):

1. `--target X` flag passed on the command line.
2. `CARVE_TARGET` environment variable.
3. `default_target` from `carve.toml`.
4. Hard-coded fallback `"dev"` only if `carve.toml` is missing entirely (early-init / pre-init scenarios).

Resolution is implemented as a single helper:

```python
def resolve_active_target(
    cli_flag: str | None,
    config: Config,
    env: Mapping[str, str] = os.environ,
) -> str:
    if cli_flag:
        return cli_flag
    if env.get("CARVE_TARGET"):
        return env["CARVE_TARGET"]
    if config.project.default_target:
        return config.project.default_target
    return "dev"
```

### Validation

After resolution, the active target must exist as `targets/<name>/`. If missing, the CLI exits 2 with a message listing the existing targets:

```
Error: target "stagung" does not exist.
Available targets: dev, staging, prod
Create one with: carve target create stagung
```

Validation runs once at the top of every command that reads/writes target state — surfaced via a typer dependency (`active_target = Depends(require_target)`).

## `carve target` subcommands

### `carve target create <name>`

Scaffolds a new target directory:

```
targets/<name>/
├── .env.example
├── connections.toml      # commented template; same shape as M1.1-01's
└── el/                   # empty
```

Implementation:

- Refuse with exit 2 if `targets/<name>/` already exists, unless `--force` is passed (which clobbers, with a confirmation prompt).
- Validate `<name>` matches `^[a-z][a-z0-9_]*$` (same rule as pipeline names from M1.1-06's backfill validation). Reject otherwise with a clear error.
- Reuse the existing M1.1-01 template content for `connections.toml` and `.env.example` — extracted into `write_target_skeleton(name, root)` shared with `carve init` (P1-03).
- After creating, append `targets/<name>/.env` to `.gitignore` if not already covered by the `targets/*/.env` glob.
- Print a success message naming the next steps:
  ```
  Created target "staging" at targets/staging/.

  Next steps:
    1. Edit targets/staging/connections.toml
    2. Copy .env.example to .env and fill in secrets:
         cp targets/staging/.env.example targets/staging/.env
    3. Run a build against this target:
         carve build <plan_id> --target staging
  ```

### `carve target list`

Prints a `rich`-formatted table of existing targets:

```
Targets

  Name      Default   .env     EL artifacts   Last activity
  ────────────────────────────────────────────────────────────
  dev       *         ✓        3              2 minutes ago
  staging             ✗        0              —
  prod                ✓        1              3 days ago
```

Columns:

- **Name** — directory name under `targets/`.
- **Default** — `*` if it matches `default_target`.
- **.env** — `✓` if `targets/<name>/.env` exists; `✗` otherwise (helps spot adoption-time misses).
- **EL artifacts** — count of subdirectories under `targets/<name>/el/`.
- **Last activity** — most recent `Run` row touching this target (`runs.target = <name>`); rendered as relative time.

Empty state (no targets directory or empty): "No targets yet. Run `carve init` or `carve target create <name>`."

### `carve target show <name>`

Detailed view of one target:

```
Target: staging
  Default: no
  .env:    targets/staging/.env (✓ exists)

Connections (from targets/staging/connections.toml)
  snowflake.staging:
    account:   <redacted>
    role:      TRANSFORMER_STAGING
    warehouse: TRANSFORMER_WH
    database:  ANALYTICS_STAGING

EL artifacts
  iowa_liquor (last deployed 3 days ago, last run 2 hours ago — success)
  salesforce_opps (built but never deployed)

(No pipelines or schedules — Pillars 3 and 4 not yet adopted.)
```

Connection values are fetched from `connections.toml` and passed through a redaction helper before printing — `account` is redacted when shown via `carve target show`, but kept visible in `connections.toml` itself (where it's already committed). Anything that came from `${ENV_VAR}` substitution is shown as `<from .env>` rather than the resolved value.

### `carve target rename <old> <new>`

Renames a target directory. Steps:

1. Validate `<new>` matches the naming regex; refuse if a `targets/<new>/` already exists.
2. `git mv targets/<old> targets/<new>` — preserves history. If the working tree isn't a git repo, fall back to a regular `mv` with a warning.
3. If `<old>` was `default_target`, update `default_target` in `carve.toml` to `<new>`.
4. Print a summary noting what moved.

Refused cases (exit 2 with a clear message):

- `<old>` does not exist.
- `<new>` already exists.
- `<new>` doesn't match the naming regex.

The "refuse if any open PRs reference the old name" check from the stub is **deferred to P1-09** — Pillar 1's deploy spec is the natural home for PR-awareness. P1-01 just renames; users with open carve-deploy PRs are warned by `carve target rename` to close those PRs first ("Open carve PRs may reference targets/<old>/; close or rebase them after rename.") and proceed.

### `carve target delete <name>`

Removes a target directory. Safety rails:

- Refuse if `<name>` is `default_target`, unless `--force` is passed AND a follow-up `--no-default-warning` confirms intent (this is intentionally awkward; deleting the default is a foot-gun).
- Refuse if `targets/<name>/` has any artifacts (`el/`, `pipelines/`, `schedules/` non-empty), unless `--force`.
- Unconditional confirmation prompt: `Delete target "staging" and all its contents? [y/N]`. `--yes` skips.
- After deletion: remove the `targets/<name>/.env` line from `.gitignore` if it was added explicitly by name (the `targets/*/.env` glob stays).

Explicitly **not in scope:**

- Dropping anything in the target's Snowflake account. The user removes Snowflake state separately (or leaves it; the target directory deletion is purely local).
- Cleaning up open PRs that referenced the deleted target. They'll fail their next `provision` / `verify` step; the user closes them manually.

Pipeline lifecycle (disable/archive/restore) is a Pillar 4 / M3 concern; `delete` is the only artifact-removal verb Pillar 1 ships.

## Top-level `--target` flag

Every Carve subcommand that reads or writes target state accepts `--target X` as a uniform top-level option. Implementation: typer callback on the root app that captures `--target` and stows it for downstream commands to read via `resolve_active_target`.

Commands that accept `--target`:

- `carve plan` (P1-02; the plan agent inspects schemas in this target)
- `carve build` (P1-02; build inherits from plan, but `--target` can override at build time with the conflict-rejection logic from P1-02)
- `carve el run` (P1-08)
- `carve el deploy` (P1-09; takes both `--from` and `--to` instead of a single `--target` — natural for promotion)
- `carve target show <name>` — `<name>` is the positional, but other `target` subcommands like `target list` honor a default if the user wants pipe-able output (rare; defer)

Commands that do NOT accept `--target`:

- `carve init` (no targets exist yet at init time; `targets/dev/` is created unconditionally)
- `carve version`
- `carve target create <name>` — `<name>` is the positional argument

## Cross-cutting integration

### Config schema

`carve.toml`'s `[project]` section already has `default_target` (M1-02). This spec confirms it's authoritative: `Config.project.default_target`. No schema changes.

### State store

No DB schema changes in this spec. Later Pillar 1 specs (`carve el run`, `carve el deploy`) add a `target` column to the `runs` table; this spec doesn't introduce that itself but documents that `runs.target` will exist by the end of Pillar 1 (added in P1-08 or P1-09 — TBD which spec adds it).

### Dotenv

P1-04 (per-target dotenv) reads `targets/<active>/.env`. This spec defines the `<active>` resolution; P1-04 consumes it. The two specs are deliberately split because the resolution logic is reused beyond dotenv (path resolution for artifacts, connections.toml location, etc.).

### Init layout

P1-03 (init) calls `write_target_skeleton("dev", project_root)` from this spec's scaffolding helper. `carve init` and `carve target create` write the same skeleton; the only difference is whether `targets/dev/` was the first target created (`init`) or an additional one (`target create`).

## Implementation

### File-level changes

New files:

- `src/carve/cli/commands/target/__init__.py` — typer subgroup wiring `create`, `list`, `show`, `rename`, `delete`.
- `src/carve/cli/commands/target/create.py`
- `src/carve/cli/commands/target/list.py`
- `src/carve/cli/commands/target/show.py`
- `src/carve/cli/commands/target/rename.py`
- `src/carve/cli/commands/target/delete.py`
- `src/carve/core/targets/__init__.py`
- `src/carve/core/targets/resolution.py` — `resolve_active_target(...)`, `require_target(...)` typer dependency.
- `src/carve/core/targets/scaffolding.py` — `write_target_skeleton(name, root)`, `target_exists(name, root)`, naming-regex validator. Reuses M1.1-01's template strings (extract from `cli/commands/init.py` into shared module).
- `tests/cli/commands/target/test_create.py`
- `tests/cli/commands/target/test_list.py`
- `tests/cli/commands/target/test_show.py`
- `tests/cli/commands/target/test_rename.py`
- `tests/cli/commands/target/test_delete.py`
- `tests/core/targets/test_resolution.py`
- `tests/core/targets/test_scaffolding.py`

Modified files:

- `src/carve/cli/main.py` — register `target` subgroup; add top-level `--target` callback option; add `--target` -> `resolve_active_target` plumbing.
- `src/carve/cli/commands/init.py` — extract its inline target-skeleton logic into `core/targets/scaffolding.py` (no behavior change; refactor only).
- `tests/test_cli.py` — `EXPECTED_COMMANDS` gains `target` group.

### Naming-regex validator

```python
TARGET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

def validate_target_name(name: str) -> None:
    if not TARGET_NAME_RE.fullmatch(name):
        raise InvalidTargetNameError(
            f"Target name {name!r} must match {TARGET_NAME_RE.pattern} "
            "(lowercase, alphanumeric and underscores, starting with a letter)."
        )
```

Same regex as pipeline names from M1.1-06; consistency is intentional.

## Tests

- `test_resolution_cli_flag_wins` — `--target` over env over config over fallback.
- `test_resolution_env_var` — `CARVE_TARGET` honored when no flag.
- `test_resolution_default_target` — falls through to `default_target` when no flag/env.
- `test_resolution_hardcoded_fallback` — returns `"dev"` when `carve.toml` is missing.
- `test_require_target_raises_on_missing` — `targets/<resolved>/` not present → exit 2 with the listing-of-existing-targets message.
- `test_target_create_writes_skeleton` — `targets/<new>/` has the expected files.
- `test_target_create_refuses_existing` — without `--force`, exits 2.
- `test_target_create_refuses_invalid_name` — naming-regex check.
- `test_target_create_appends_gitignore` — `targets/<new>/.env` lands in `.gitignore` (or is covered by glob).
- `test_target_list_marks_default` — default target row marked with `*`.
- `test_target_list_empty_state` — empty `targets/` shows the empty-state message.
- `test_target_show_redacts_account` — `account` is redacted in the printed connection summary.
- `test_target_show_lists_el_artifacts` — counts subdirectories under `el/`.
- `test_target_rename_moves_directory` — `git mv`-equivalent applied.
- `test_target_rename_updates_default_target` — `default_target` in `carve.toml` updated when renaming the default.
- `test_target_rename_refuses_if_destination_exists` — exits 2.
- `test_target_delete_default_target_refused` — without `--force` + `--no-default-warning`, exits 2.
- `test_target_delete_non_empty_refused` — without `--force`, exits 2.
- `test_target_delete_happy_path` — empty target with confirmation removes the directory.
- `test_top_level_target_flag_wired` — running `carve --target staging el list` resolves to `staging`.
- `test_init_uses_target_skeleton` — `carve init` produces `targets/dev/` with the expected files (regression for the refactor).

## Acceptance criteria

- `carve target create <name>` creates `targets/<name>/` with the documented skeleton (`.env.example`, `connections.toml`, `el/`); fails on collision and on invalid name.
- `carve target list` shows all targets with the default flag, .env presence, and EL artifact count.
- `carve target show <name>` shows connection summary (with `account` redacted) and EL artifact list with last-deploy/last-run timestamps where available.
- `carve target rename <old> <new>` moves the directory via `git mv` (or `mv` outside a repo) and updates `default_target` if the renamed target was default.
- `carve target delete <name>` removes the directory with the safety rails described.
- `--target X` flag, `CARVE_TARGET` env var, and `default_target` config resolve correctly; missing target exits 2 with a helpful message.
- `carve init` (P1-03) and `carve target create` (this spec) share the same skeleton-writing helper — refactored without regressing the M1.1-01 template content.
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover all subcommands and resolution paths.

## Files this spec produces

(Summary of the file-level changes section above.)

New: 7 source files + 1 typer subgroup, 7 test files.
Modified: `cli/main.py`, `cli/commands/init.py` (extract-only refactor), `tests/test_cli.py`.
No DB migrations.

## Out of scope

- Per-target connection schemas — `connections.toml` shape is M1-02's territory; this spec just consumes the existing schema and locates the file under `targets/<name>/`.
- Per-target secret loading — P1-04.
- Cross-target promotion / deploy — P1-09.
- Target-aware artifact resolution for runs and deploys — handled by P1-08 (`carve el run`) and P1-09 (`carve el deploy`); each pillar's verbs are responsible for their own path resolution against the active target.
- Multi-target operations in one command (`--targets staging,prod`) — defer.
- Target archive / disable lifecycle — Pillar 4 / M3 alongside pipeline lifecycle.
- Open-PR awareness in `target rename` — moved to P1-09 where PR awareness lives anyway.
- Snowflake-side cleanup on `target delete` — never in scope; users manage Snowflake state separately.

## What this enables

- **Every Pillar 1+ artifact has a single, predictable home** based on which target it belongs to. Reading the filesystem is the canonical answer to "what's deployed where."
- **`--target` is a uniform CLI affordance** — every operational verb honors it the same way; users learn it once.
- **Multi-environment workflows are first-class** without prescribing a specific topology. Teams can have just `dev`; or `dev`/`prod`; or `dev`/`staging`/`prod`/`eu_prod` — each is just a directory.
- **Future pillars compose cleanly** — Pillar 3's `pipelines/` and Pillar 4's `schedules/` slot into the same per-target tree without further structural design.
