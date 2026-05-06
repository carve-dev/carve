# P1-03 — Init for per-target layout

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 0.5 day
**Dependencies:** M1.1-01 (existing init templates), P1-01 (target system)
**Lineage:** Continues **M1.1-01** (init config templates). The templated content is preserved verbatim — only the file destinations evolve to match the centralized config + per-target artifact-folder model from P1-01. Existing `_write_if_missing` helper from M1-01 is reused. Brownfield dbt detection from accepted **M2-07** explicitly does **not** carry forward to Pillar 1; it lives in Pillar 2 alongside the dbt agent.

## Purpose

Refactor `carve init` so a fresh project produces the centralized configuration + per-target artifact layout that Pillar 1 (and every later pillar) expects, while preserving every templated string M1.1-01 already shipped.

## Greenfield layout produced by `carve init`

In an empty directory, `carve init` writes:

```
carve.toml                         # [project] name + version + default_target = "dev"
carve/
  connections.toml                 # ONE file, one [snowflake.<target>] section per target.
                                   # Init seeds it with [snowflake.dev].
  runner.toml                      # commented template (project-wide; M1.1-01 verbatim)
  models.toml                      # commented template (project-wide; M1.1-01 verbatim)
  agents/                          # empty (reserved for a future agent registry)
targets/
  dev/
    el/                            # empty (Pillar 1 artifact destination)
.env                                # NOT created by init
.env.example                       # tracked; project-wide vars + DEV_* prefixed Snowflake vars
.gitignore                         # .env, .carve/, *.sqlite, *.sqlite3
.carve/
  state.db                         # initialized SQLite state store (M1-03 schema; P1-02 migration 0004 brings it to current)
```

**Centralized vs per-target.** All configuration lives at the project level (`carve/connections.toml`, `.env`, `carve/runner.toml`, `carve/models.toml`). Only **deployable artifacts** live per-target (`targets/<name>/el/`). Adding a target via `carve target create <name>` adds a `[snowflake.<name>]` section to `connections.toml`, appends `<NAME>_*` lines to `.env.example`, and creates `targets/<name>/el/` — see P1-01.

**What's NOT in the greenfield layout** (intentional omissions, each with the spec that owns the artifact):

- No `carve/server.toml` — added if/when M2-10's FastAPI server lands (post-Pillar-4 milestone or scrapped).
- No `carve/conventions.md` — Pillar 2's convention inference produces it; greenfield projects start without one.
- No `dbt/` scaffold — Carve does not run `dbt init`; users who want dbt run it themselves and use Pillar 2's brownfield detection later.
- No `pipelines/` or `schedules/` directories under `targets/dev/` — created lazily by their pillars (Pillar 3 / Pillar 4) when their first artifact lands.
- No actual `.env` file — only `.env.example`. Init refuses to write `.env` so the user is forced to opt in by copying and editing the template (avoids accidentally committing an empty `.env` and avoids surprising users with an unexpected gitignored file).

## What's in `.env.example` at init time

```bash
# === Project-wide ===
ANTHROPIC_API_KEY=
# GITHUB_TOKEN=                          # uncomment if using `carve el deploy`

# === dev target ===
DEV_SNOWFLAKE_ACCOUNT=
DEV_SNOWFLAKE_USER=
DEV_SNOWFLAKE_PASSWORD=
DEV_SNOWFLAKE_ROLE=
DEV_SNOWFLAKE_WAREHOUSE=
DEV_SNOWFLAKE_DATABASE=
```

Sections are separated by clearly-marked `# ===` comment headers — this is what `carve target create <name>` and `carve target rename` look for when adding/renaming/removing target blocks. The format is dotenv-compatible (no parsed semantics; the `#` lines are comments).

A user who wants per-target Anthropic keys (testing different models, billing separation) sets `DEV_ANTHROPIC_API_KEY` / `PROD_ANTHROPIC_API_KEY` and references those in `carve/models.toml`. This is documented as a convention; not enforced by Pillar 1 code.

## What's in `carve/connections.toml` at init time

```toml
[snowflake.dev]
account   = "${DEV_SNOWFLAKE_ACCOUNT}"
user      = "${DEV_SNOWFLAKE_USER}"
password  = "${DEV_SNOWFLAKE_PASSWORD}"
role      = "${DEV_SNOWFLAKE_ROLE}"
warehouse = "${DEV_SNOWFLAKE_WAREHOUSE}"
database  = "${DEV_SNOWFLAKE_DATABASE}"
```

Same per-key placeholder shape as M1.1-01's commented template, just resolved to `[snowflake.dev]` and target-prefixed env-var references.

## What's in `carve.toml` at init time

```toml
[project]
name = "<directory_name>"           # detected from cwd
version = "0.0.1"
default_target = "dev"

[paths]
config_dir = "carve"
agents_dir = "carve/agents"
targets_dir = "targets"
```

The `[paths]` section gains `targets_dir = "targets"` so an advanced user can relocate `targets/` if they want (e.g. into a sub-project).

## The scaffolding helper

P1-01 introduced `src/carve/core/targets/registry.py` with TOML-edit-in-place helpers and `.env.example` block helpers. P1-03 wires them into `carve init`:

```python
def command(directory: Path = Argument(...)) -> None:
    root = directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Initializing Carve project in[/bold] {root}")

    _write_if_missing(root / "carve.toml", _carve_toml_template(default_target="dev"))
    _write_if_missing(root / "carve" / "runner.toml", RUNNER_TOML_CONTENT)
    _write_if_missing(root / "carve" / "models.toml", MODELS_TOML_CONTENT)
    _ensure_dir(root / "carve" / "agents")
    _write_if_missing(root / "carve" / "connections.toml", _connections_header_only())
    _write_if_missing(root / ".env.example", _env_example_header_only())

    # Seed the dev target via the same helper carve target create uses
    add_target_to_project("dev", root)

    _write_if_missing(root / ".gitignore", _gitignore_template())
    _initialize_state_store(root)

    console.print("[green]✓[/green] Project initialized.")
```

`add_target_to_project(name, root)` is the centralized scaffolder defined in `core/targets/registry.py` (P1-01). It:

1. Appends `[snowflake.<name>]` to `carve/connections.toml` with `${<NAME>_*}` placeholders.
2. Appends a `# === <name> target ===` block to `.env.example` with `<NAME>_*` lines.
3. Creates `targets/<name>/el/`.

`carve init` and `carve target create` call the same function — one mental model, one set of tests.

## `.gitignore` content

```
.env
.carve/
*.sqlite
*.sqlite3
```

Single-line `.env` (project-wide). Per-target `.env` files don't exist in this model.

## Migration path for existing M1.1 projects

`carve init` is idempotent — running it on an existing M1.1 project does **not** clobber files. It writes the new structure alongside the old:

- New: `targets/dev/el/`, `carve/connections.toml` (centralized form), `.env.example` (centralized form)
- Untouched: legacy `carve/connections.toml` (if it had a different shape), legacy root `.env`, `pipelines/<name>/`

A short migration recipe in `CHANGELOG.md` under v0.1.0:

```bash
# 1. Move artifacts into the per-target tree
git mv pipelines targets/dev/el

# 2. Reshape carve/connections.toml from the old M1.1 single-target shape
#    to the new multi-section centralized shape.

# 3. Rename env vars in .env to add the DEV_ prefix.

# 4. Edit carve.toml: add `default_target = "dev"` under [project]
```

We do **not** ship a `carve migrate` command in v0.1. The manual recipe is short enough that the ceremony of an automated migration isn't worth the maintenance.

M1.1-03's existing root-`.env` autoload runs unchanged — the centralized model means a single root `.env` is still what gets loaded.

## Tests

- `test_init_creates_centralized_layout` — `carve init` in an empty dir produces `carve/connections.toml` (not per-target), `.env.example` at root, and `targets/dev/el/`.
- `test_init_writes_carve_toml_with_default_target` — `[project] default_target = "dev"` is in the file.
- `test_init_seeds_dev_section_in_connections` — `carve/connections.toml` contains `[snowflake.dev]` with `${DEV_SNOWFLAKE_*}` placeholders.
- `test_init_env_example_has_project_and_dev_blocks` — `.env.example` has both the `# === Project-wide ===` and `# === dev target ===` blocks.
- `test_init_does_not_create_dotenv_file` — `.env` does NOT exist after init (only `.env.example`).
- `test_init_idempotent` — running `carve init` twice is a no-op (existing files skipped with `!` markers); `[snowflake.dev]` is not duplicated.
- `test_init_initializes_state_store` — `.carve/state.db` exists with the migration head applied.
- `test_init_gitignore_uses_root_env` — `.gitignore` contains `.env` (single line), no `targets/*/.env` glob.
- `test_init_does_not_clobber_existing_files` — pre-existing `carve.toml`, `carve/connections.toml`, etc. are preserved.
- `test_init_then_target_create_produces_two_sections` — `carve init && carve target create staging` results in `connections.toml` having both `[snowflake.dev]` and `[snowflake.staging]`.

## Acceptance criteria

- `carve init` in an empty directory produces the documented layout end-to-end (centralized config + `targets/dev/el/`).
- `carve.toml` contains `default_target = "dev"` and `targets_dir = "targets"`.
- `carve/connections.toml` is created with a single `[snowflake.dev]` section using `${DEV_SNOWFLAKE_*}` placeholders.
- `.env.example` is at the project root with `# === Project-wide ===` and `# === dev target ===` blocks.
- `.gitignore` ignores root `.env`.
- `.carve/state.db` is initialized with the current migration head (M1-03 + `0003_rename_apply_to_deploy.py` + `0004_build_entity.py`).
- `carve init` is idempotent and does not clobber existing files.
- `carve init` and `carve target create` share the same `add_target_to_project` helper; integration test verifies the post-init filesystem shape matches what a `target create` of the same name would produce on a project with no other targets.
- All existing M1.1-01 templated content (the per-key Snowflake template, the project-wide / per-target structure, the `_write_if_missing` skip-with-warning behavior) is preserved.
- `ruff` + `mypy --strict` + `pytest` stay green.

## Files this spec produces

New:

- `tests/cli/commands/test_init_centralized.py` — net-new tests for the centralized layout.

Modified:

- `src/carve/cli/commands/init.py` — refactored to call `add_target_to_project("dev", root)` from `core/targets/registry.py` (P1-01). Template strings split: project-wide ones (`RUNNER_TOML_CONTENT`, `MODELS_TOML_CONTENT`, `_carve_toml_template`, `_gitignore_template`) stay in `init.py`; target-scoped ones (`_connections_section_template`, `_env_example_block_template`) move to `core/targets/registry.py`.
- `src/carve/core/config/schema.py` — `PathsConfig` gains `targets_dir = "targets"`.
- `tests/test_cli.py` and `tests/cli/commands/test_init.py` — update assertions for the centralized layout.
- `CHANGELOG.md` — entry under `## [Unreleased]` documenting the layout change and the manual migration recipe.

## Out of scope

- Brownfield dbt detection (the M2-07 work). Lives in Pillar 2 alongside the dbt agent.
- Convention inference (Pillar 2 — M2-08).
- Interactive `carve init --interactive` (prompts for connection values up-front). Defer indefinitely.
- A `carve migrate` command for upgrading M1.1 projects. Manual recipe in CHANGELOG is enough for v0.1.
- A separate "project-level" `.env` layer split from target-prefixed vars. Single `.env` with prefixes is the v0.1 shape.
- Renaming `pipelines/` table semantics. See P1-02's misnomer note.

## What this enables

- A clean `carve init → fill in .env → carve plan → carve build → carve el run` happy path on an empty directory in under 5 minutes.
- `carve target create <name>` (P1-01) reuses the same `add_target_to_project` helper, so adding a second target is a single command that mirrors what `init` did for `dev` — no visual or behavioral drift between the two.
- The greenfield layout matches dbt's centralized-config + per-target-output pattern, so users coming from dbt understand it on sight.
