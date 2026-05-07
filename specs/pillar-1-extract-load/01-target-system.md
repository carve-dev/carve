# P1-01 — Target system

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1 day
**Dependencies:** M1-02 (config)
**Lineage:** Net-new. The per-target folder model was synthesized during this session's design discussion; no direct M1/M1.1/M2 ancestor. Foundation that the rest of Pillar 1 evolves on top of.

## Purpose

Establish the per-target folder convention that every Pillar 1+ artifact lives in, the `--target` flag pattern that every Carve verb honors, the `default_target` config in `carve.toml`, and the `carve target` subcommand family for managing target lifecycle. The rest of Pillar 1 (and every later pillar) reads from / writes to these conventions; no other spec re-defines them.

## Folder convention

A target is an environment (dev, staging, prod, qa, eu_prod, etc.) plus everything Carve needs to interact with it. **Configuration is centralized; only deployable artifacts live per-target.** This matches dbt's `profiles.yml` + per-target outputs pattern — users coming from dbt won't be surprised.

```
project-root/
├── carve.toml                          # default_target = "dev"
├── carve/
│   ├── connections.toml                # tracked; ONE file with one [snowflake.<target>] section per target
│   ├── runner.toml                     # project-wide
│   ├── models.toml                     # project-wide
│   └── agents/                         # reserved for an agent registry
├── .env                                # gitignored; project-wide + target-prefixed secrets
├── .env.example                        # tracked; template
├── targets/
│   └── <name>/                         # purely an artifact directory
│       ├── el/                         # Pillar 1 artifacts (created in this spec)
│       │   └── <artifact_name>/        # populated by `carve build`
│       │       ├── main.py
│       │       └── requirements.txt
│       └── snowflake/                  # Pillar 1 generated DDL (created lazily by P1-06)
│           └── <artifact_name>.sql
└── .gitignore                          # includes `.env`
```

**Where each kind of state lives:**

| State | Location | Why |
|---|---|---|
| Connection structure (account, role, warehouse, database) per target | `carve/connections.toml`, one `[snowflake.<target>]` section each | One file mirrors dbt's `profiles.yml`; less file management; user picks the target via `--target` and Carve resolves the right section |
| Secrets per target | `.env` with target-prefixed vars (`DEV_SNOWFLAKE_USER`, `PROD_SNOWFLAKE_USER`, etc.) | Single file; can hold multiple targets via prefixes |
| Project-wide secrets (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`) | Same `.env`, unprefixed | Shared across targets by default; per-target overrides via `<TARGET>_ANTHROPIC_API_KEY` if a user wants different keys per environment |
| Deployable artifacts (EL scripts, future pipeline defs, future schedules) | `targets/<name>/el/`, `targets/<name>/pipelines/`, `targets/<name>/schedules/` | Filesystem mirrors what's deployed where; promotion = file copy between target folders |
| Generated DDL per artifact | `targets/<name>/snowflake/<artifact>.sql` | Same logic — the DDL applies to a specific target |

**Reserved subdirectory names** under `targets/<name>/` that later pillars own. They are *not* created by this spec — each pillar creates its own subdirectory the first time it lands an artifact:

- `targets/<name>/pipelines/` — Pillar 3
- `targets/<name>/schedules/` — Pillar 4

Empty placeholder directories are explicitly avoided. A target's tree mirrors what's actually deployed to that target, so users can reason about state by reading the filesystem.

### Sample `carve/connections.toml` (multi-target)

```toml
[snowflake.dev]
account   = "${DEV_SNOWFLAKE_ACCOUNT}"
user      = "${DEV_SNOWFLAKE_USER}"
password  = "${DEV_SNOWFLAKE_PASSWORD}"
role      = "${DEV_SNOWFLAKE_ROLE}"
warehouse = "${DEV_SNOWFLAKE_WAREHOUSE}"
database  = "${DEV_SNOWFLAKE_DATABASE}"

[snowflake.prod]
account   = "${PROD_SNOWFLAKE_ACCOUNT}"
user      = "${PROD_SNOWFLAKE_USER}"
password  = "${PROD_SNOWFLAKE_PASSWORD}"
role      = "${PROD_SNOWFLAKE_ROLE}"
warehouse = "${PROD_SNOWFLAKE_WAREHOUSE}"
database  = "${PROD_SNOWFLAKE_DATABASE}"
```

### Sample `.env`

```bash
# === Project-wide ===
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...

# === dev target ===
DEV_SNOWFLAKE_ACCOUNT=dev-acc.us-east-2
DEV_SNOWFLAKE_USER=dev_user
DEV_SNOWFLAKE_PASSWORD=...
DEV_SNOWFLAKE_ROLE=TRANSFORMER_DEV
DEV_SNOWFLAKE_WAREHOUSE=DEV_WH
DEV_SNOWFLAKE_DATABASE=ANALYTICS_DEV

# === prod target ===
PROD_SNOWFLAKE_ACCOUNT=prod-acc.us-east-2
PROD_SNOWFLAKE_USER=prod_user
# ...
```

If a user wants per-target Anthropic keys (testing different models per environment, billing separation, etc.), they can set `DEV_ANTHROPIC_API_KEY` / `PROD_ANTHROPIC_API_KEY` and reference those in `carve/models.toml`. Pillar 1 doesn't enforce this; it just supports the pattern.

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

After resolution, the active target must be defined as a `[snowflake.<name>]` section in `carve/connections.toml`. If missing, the CLI exits 2 with a message listing the existing targets:

```
Error: target "stagung" not defined in carve/connections.toml.
Available targets: dev, staging, prod
Create one with: carve target create stagung
```

> **Updated during implementation (2026-05-07):** `require_target` shipped as a plain validator (`require_target(name, available) -> None`, raises `TargetResolutionError` on miss) rather than a typer-dependency callable. The declarative `active_target = Depends(require_target)` wiring on subcommands was punted; commands that need an active target call `resolve_active_target(...)` + `require_target(...)` directly. The `--target` flag value is shared from the typer root callback to subcommands via the `carve.cli.main.ACTIVE_TARGET_FLAG` module-level slot.

Validation runs once at the top of every command that reads/writes target state. The validator is a plain function (`require_target(name, available)`) rather than a typer dependency; subcommands call `resolve_active_target(...)` followed by `require_target(...)` explicitly.

## `carve target` subcommands

### `carve target create <name>`

Adds a new target by **(a)** appending a `[snowflake.<name>]` section to `carve/connections.toml` and **(b)** appending target-prefixed env-var lines to `.env.example`, then creating the per-target artifact directory:

```
carve/connections.toml          ← gains a new [snowflake.<name>] section (with ${<NAME>_*} placeholders)
.env.example                    ← gains commented `# === <name> target ===` block with <NAME>_* lines
targets/<name>/                 ← new directory
└── el/                         ← empty (Pillar 1 destination)
```

Implementation:

- Refuse with exit 2 if a `[snowflake.<name>]` section already exists in `carve/connections.toml`, unless `--force` (with confirmation prompt).
- Validate `<name>` matches `^[a-z][a-z0-9_]*$` (same rule as pipeline names from M1.1-06's backfill validation). Reject otherwise with a clear error.
- Use a TOML edit-in-place helper (preserves existing sections + comments) — `tomlkit` library, since stdlib `tomllib` is read-only. Append the new section at the end of the file with a leading blank line.
- Append a clearly-marked block to `.env.example`:
  ```
  
  # === <name> target ===
  <NAME>_SNOWFLAKE_ACCOUNT=
  <NAME>_SNOWFLAKE_USER=
  <NAME>_SNOWFLAKE_PASSWORD=
  <NAME>_SNOWFLAKE_ROLE=
  <NAME>_SNOWFLAKE_WAREHOUSE=
  <NAME>_SNOWFLAKE_DATABASE=
  ```
- Create `targets/<name>/el/` (empty).
- The `.gitignore` already ignores root `.env` (single line, set up by `carve init`); no change needed for new targets.
- Print a success message naming the next steps:
  ```
  Created target "staging".

  Next steps:
    1. Add STAGING_* values to .env (see .env.example for the list)
    2. Review the [snowflake.staging] section in carve/connections.toml
    3. Run a build against this target:
         carve build <plan_id> --target staging
  ```

### `carve target list`

Prints a `rich`-formatted table of existing targets. Authoritative source is `carve/connections.toml` — every `[snowflake.<name>]` section is a defined target. The `targets/<name>/` directory presence is reported alongside.

> **Updated during implementation (2026-05-07):** the `Last activity` column was punted from this release because `runs.target` does not exist yet (added in P1-02 with the Build entity migration). The shipped table has the five columns: Name / Default / Secrets / Artifacts dir / EL artifacts. The column will be reintroduced once `runs.target` lands.

```
Targets

  Name      Default   Secrets    Artifacts dir   EL artifacts
  ────────────────────────────────────────────────────────────
  dev       *         ✓ all set  ✓ exists        3
  staging             ✗ missing  ✓ exists        0
  prod                ✓ all set  ✗ missing       —
```

Columns:

- **Name** — section name from `carve/connections.toml` (`[snowflake.<name>]`).
- **Default** — `*` if it matches `default_target`.
- **Secrets** — `✓ all set` if every `${<NAME>_*}` env var the section references is present in the loaded environment; `✗ missing` otherwise (helps spot adoption-time misses without leaking which specific vars).
- **Artifacts dir** — `✓ exists` if `targets/<name>/` exists; `✗ missing` otherwise. Missing-but-defined targets are valid (a target can exist in config without artifacts yet).
- **EL artifacts** — count of subdirectories under `targets/<name>/el/`. Dash if the artifacts dir is missing.

Empty state (no targets directory or empty): "No targets yet. Run `carve init` or `carve target create <name>`."

### `carve target show <name>`

> **Updated during implementation (2026-05-07):** EL-artifact rows render as bare directory names rather than annotated with last-deploy/last-run timestamps, for the same reason `target list` lost its `Last activity` column — `runs.target` does not exist yet (added in P1-02). The annotations will return once the column lands.

Detailed view of one target:

```
Target: staging
  Default:        no
  Defined in:     carve/connections.toml [snowflake.staging]
  Secrets:        ✓ all set (STAGING_SNOWFLAKE_USER, _PASSWORD, _ROLE, _WAREHOUSE, _DATABASE, _ACCOUNT)
  Artifacts dir:  targets/staging/ (✓ exists)

Connection (resolved)
  snowflake.staging:
    account:   <from STAGING_SNOWFLAKE_ACCOUNT>
    role:      TRANSFORMER_STAGING
    warehouse: TRANSFORMER_WH
    database:  ANALYTICS_STAGING

EL artifacts
  iowa_liquor
  salesforce_opps

(No pipelines or schedules — Pillars 3 and 4 not yet adopted.)
```

Connection values are fetched from `carve/connections.toml`'s `[snowflake.<name>]` section. Anything that came from `${ENV_VAR}` substitution is shown as `<from <var>>` rather than the resolved value — never prints the actual secret. Non-substituted values (literal `account = "..."` for instance) display as-is, since they're already in source control.

### `carve target rename <old> <new>`

Renames a target across all its locations:

1. Validate `<new>` matches the naming regex; refuse if a `[snowflake.<new>]` section already exists in `carve/connections.toml` or `targets/<new>/` already exists.
2. **Connection section:** rename `[snowflake.<old>]` → `[snowflake.<new>]` in `carve/connections.toml` (preserve comments, key order, blank lines).
3. **Env vars in `.env.example`:** rewrite `<OLD>_*` lines to `<NEW>_*`. Print a friendly nudge to the user that they need to rename the same vars in their actual `.env` file (Carve does not edit `.env` since it's not in version control).
4. **Artifact directory:** if `targets/<old>/` exists, `git mv targets/<old> targets/<new>`. If not in a git repo, plain `mv` with a warning.
5. **Default target:** if `<old>` was `default_target`, update `default_target` in `carve.toml`.
6. Print a summary listing what moved + the reminder to update `.env`.

Refused cases (exit 2 with a clear message):

- `<old>` is not defined (no `[snowflake.<old>]` section).
- `<new>` already defined or `targets/<new>/` exists.
- `<new>` doesn't match the naming regex.

The "refuse if any open PRs reference the old name" check is **deferred to P1-08** — Pillar 1's deploy spec is the natural home for PR-awareness. P1-01 just renames; users with open carve-deploy PRs are warned by `carve target rename` to close those PRs first.

### `carve target delete <name>`

Removes a target across all its locations:

1. **Connection section:** remove `[snowflake.<name>]` from `carve/connections.toml`.
2. **Env-var lines:** remove the `# === <name> target ===` block + the `<NAME>_*` lines from `.env.example`. Print a nudge to remove the same lines from the user's `.env` (Carve doesn't edit `.env`).
3. **Artifact directory:** remove `targets/<name>/` if it exists.

Safety rails:

- Refuse if `<name>` is `default_target`, unless `--force` is passed AND a follow-up `--no-default-warning` confirms intent (this is intentionally awkward; deleting the default is a foot-gun).
- Refuse if `targets/<name>/` has any artifacts (`el/`, `pipelines/`, `schedules/` non-empty), unless `--force`.
- Unconditional confirmation prompt: `Delete target "staging" — section in connections.toml, lines in .env.example, and targets/staging/? [y/N]`. `--yes` skips.

Explicitly **not in scope:**

- Dropping anything in the target's Snowflake account. The user removes Snowflake state separately (or leaves it; the deletion is purely local).
- Cleaning up open PRs that referenced the deleted target. They'll fail their next `deploy` / `verify` step; the user closes them manually.

Pipeline lifecycle (disable/archive/restore) is a Pillar 4 / M3 concern; `delete` is the only artifact-removal verb Pillar 1 ships.

## Top-level `--target` flag

Every Carve subcommand that reads or writes target state accepts `--target X` as a uniform top-level option. Implementation: typer callback on the root app that captures `--target` and stows it for downstream commands to read via `resolve_active_target`.

Commands that accept `--target`:

- `carve plan` (P1-02; the plan agent inspects schemas in this target)
- `carve build` (P1-02; build inherits from plan, but `--target` can override at build time with the conflict-rejection logic from P1-02)
- `carve el run` (P1-07)
- `carve el deploy` (P1-08; takes both `--from` and `--to` instead of a single `--target` — natural for promotion)
- `carve target show <name>` — `<name>` is the positional, but other `target` subcommands like `target list` honor a default if the user wants pipe-able output (rare; defer)

Commands that do NOT accept `--target`:

- `carve init` (no targets exist yet at init time; `targets/dev/` is created unconditionally)
- `carve version`
- `carve target create <name>` — `<name>` is the positional argument

## Cross-cutting integration

### Config schema

`carve.toml`'s `[project]` section already has `default_target` (M1-02). This spec confirms it's authoritative: `Config.project.default_target`. No schema changes.

### State store

No DB schema changes in this spec. Later Pillar 1 specs (`carve el run`, `carve el deploy`) add a `target` column to the `runs` table; this spec doesn't introduce that itself but documents that `runs.target` will exist by the end of Pillar 1 (added in P1-07 or P1-08 — TBD which spec adds it).

### Dotenv

Root `.env` is loaded once at CLI startup (M1.1-03's existing autoload — unchanged). Because all targets' secrets live in the same file (target-prefixed), there's no per-target `.env` switching. The active target only affects which `[snowflake.<target>]` *section* of `connections.toml` is selected; the env vars referenced by that section are already in the loaded environment.

This deliberately collapses what was originally P1-04 — the spec dissolves entirely with the centralized model.

### Init layout

P1-03 (init) calls `add_target_to_project("dev", root)` from this spec's registry helper. `carve init` and `carve target create` use the same helper — `init` is just "the first invocation that also creates the surrounding files (`carve.toml`, the `carve/` directory, the project-wide section of `.env.example`, etc.)."

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
- `src/carve/core/targets/registry.py` — TOML-edit-in-place helpers built on `tomlkit`: `add_target_section(name, conn_path)`, `remove_target_section(name, conn_path)`, `rename_target_section(old, new, conn_path)`, `list_target_sections(conn_path)`. Plus parallel helpers for the `.env.example` text manipulation. Plus the high-level `add_target_to_project(name, root)` that orchestrates section + env-example block + artifact dir creation.
- `tests/cli/commands/target/test_create.py`
- `tests/cli/commands/target/test_list.py`
- `tests/cli/commands/target/test_show.py`
- `tests/cli/commands/target/test_rename.py`
- `tests/cli/commands/target/test_delete.py`
- `tests/core/targets/test_resolution.py`
- `tests/core/targets/test_registry.py`

Modified files:

- `src/carve/cli/main.py` — register `target` subgroup; add top-level `--target` callback option; add `--target` -> `resolve_active_target` plumbing (the resolved flag is shared with subcommands via the module-level `ACTIVE_TARGET_FLAG` slot).
- `src/carve/cli/commands/init.py` — refactored to call `add_target_to_project("dev", root)` from the registry instead of writing a per-target `connections.toml`. Init now writes the `[snowflake.dev]` section into `carve/connections.toml` (previously left it empty/minimal); this is the natural consequence of sharing the helper with `carve target create`.
- `src/carve/core/config/schema.py` — add `targets_dir: str = "targets"` to `PathsConfig` so registry helpers don't hard-code the path. Default is backward compatible.
- `pyproject.toml` — add `tomlkit>=0.13` runtime dependency (preserves comments + ordering on TOML edits).
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
- `test_target_create_appends_section_to_connections` — `[snowflake.<new>]` is appended to `carve/connections.toml` with `${<NAME>_*}` placeholders, preserving prior sections + comments.
- `test_target_create_appends_block_to_env_example` — `# === <new> target ===` block + `<NAME>_*` lines appended to `.env.example`.
- `test_target_create_creates_artifact_dir` — `targets/<new>/el/` exists.
- `test_target_create_refuses_existing_section` — without `--force`, exits 2 if `[snowflake.<name>]` already in `connections.toml`.
- `test_target_create_refuses_invalid_name` — naming-regex check.
- `test_target_list_marks_default` — default target row marked with `*`.
- `test_target_list_empty_state` — no `[snowflake.*]` sections shows the empty-state message.
- `test_target_list_secrets_status` — `Secrets` column reports `✓ all set` / `✗ missing` correctly.
- `test_target_show_uses_from_var_for_substituted` — substituted values render as `<from <VAR_NAME>>`, never the resolved secret.
- `test_target_show_lists_el_artifacts` — counts subdirectories under `targets/<name>/el/`.
- `test_target_rename_renames_section` — `[snowflake.<old>]` → `[snowflake.<new>]` in connections.toml; comments + blank lines preserved.
- `test_target_rename_renames_env_example_lines` — `<OLD>_*` lines rewritten to `<NEW>_*`.
- `test_target_rename_renames_artifacts_dir` — `git mv targets/<old> targets/<new>` applied when artifacts dir exists.
- `test_target_rename_updates_default_target` — `default_target` in `carve.toml` updated when renaming the default.
- `test_target_rename_refuses_if_destination_exists` — exits 2.
- `test_target_delete_removes_section` — `[snowflake.<name>]` gone from connections.toml after delete.
- `test_target_delete_removes_env_example_block` — `# === <name> target ===` block gone from `.env.example`.
- `test_target_delete_removes_artifacts_dir` — `targets/<name>/` removed.
- `test_target_delete_default_target_refused` — without `--force` + `--no-default-warning`, exits 2.
- `test_target_delete_non_empty_refused` — without `--force`, exits 2.
- `test_top_level_target_flag_wired` — running `carve --target staging el list` resolves to `staging`.
- `test_init_uses_add_target_to_project` — `carve init` produces the same `[snowflake.dev]` section + `# === dev target ===` block + `targets/dev/el/` that `carve target create dev` would (regression for the refactor).

## Acceptance criteria

- `carve target create <name>` adds `[snowflake.<name>]` to `carve/connections.toml`, appends a `# === <name> target ===` block to `.env.example`, and creates `targets/<name>/el/`. Fails on section collision and on invalid name.
- `carve target list` shows all targets with the default flag, secrets status, artifacts-dir presence, EL artifact count. (The `Last activity` column was punted; reintroduced when `runs.target` lands in P1-02.)
- `carve target show <name>` shows connection summary with `${VAR}`-substituted values rendered as `<from VAR>` (never the resolved secret) and EL artifact list. (Per-artifact last-deploy/last-run annotations were punted; reintroduced when `runs.target` lands in P1-02.)
- `carve target rename <old> <new>` rewrites the section in `connections.toml`, rewrites `<OLD>_*` → `<NEW>_*` lines in `.env.example`, `git mv`s `targets/<old>` → `targets/<new>` if the artifacts dir exists, and updates `default_target` if applicable.
- `carve target delete <name>` removes the section + env-example block + artifacts dir, with the safety rails described.
- `--target X` flag, `CARVE_TARGET` env var, and `default_target` config resolve correctly; missing target exits 2 with a helpful message.
- `carve init` (P1-03) and `carve target create` (this spec) share the same `add_target_to_project` helper.
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover all subcommands and resolution paths.

## Files this spec produces

(Summary of the file-level changes section above.)

New: 9 source files (5 target subcommands + `target/__init__.py` typer subgroup + `core/targets/__init__.py` + `resolution.py` + `registry.py`), 7 test files.
Modified: `cli/main.py`, `cli/commands/init.py` (refactored to share `add_target_to_project`), `core/config/schema.py` (added `PathsConfig.targets_dir`), `pyproject.toml` (added `tomlkit>=0.13`), `tests/test_cli.py`.
No DB migrations.

## Out of scope

- Per-target connection schemas — `connections.toml` shape is M1-02's territory; this spec just consumes the existing schema and adds multi-section support via `tomlkit`.
- Per-target dotenv loading — dissolved during spec review. M1.1-03's existing root `.env` autoload covers the centralized model with no changes.
- Cross-target promotion / deploy — P1-08.
- Target-aware artifact resolution for runs and deploys — handled by P1-07 (`carve el run`) and P1-08 (`carve el deploy`); each pillar's verbs are responsible for their own path resolution against the active target.
- Multi-target operations in one command (`--targets staging,prod`) — defer.
- Target archive / disable lifecycle — Pillar 4 / M3 alongside pipeline lifecycle.
- Open-PR awareness in `target rename` — moved to P1-08 where PR awareness lives anyway.
- Snowflake-side cleanup on `target delete` — never in scope; users manage Snowflake state separately.

## What this enables

- **Every Pillar 1+ artifact has a single, predictable home** based on which target it belongs to. Reading the filesystem is the canonical answer to "what's deployed where."
- **`--target` is a uniform CLI affordance** — every operational verb honors it the same way; users learn it once.
- **Multi-environment workflows are first-class** without prescribing a specific topology. Teams can have just `dev`; or `dev`/`prod`; or `dev`/`staging`/`prod`/`eu_prod` — each is just a directory.
- **Future pillars compose cleanly** — Pillar 3's `pipelines/` and Pillar 4's `schedules/` slot into the same per-target tree without further structural design.
