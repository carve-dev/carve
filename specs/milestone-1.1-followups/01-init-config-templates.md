# M1.1-01 — `carve init` config templates

**Milestone:** 1.1 — M1 follow-ups
**Estimated effort:** 0.25 day
**Dependencies:** M1-01 (CLI foundation), M1-02 (config loader)

## Purpose

Replace the one-line comment placeholders that `carve init` writes for `connections.toml`, `models.toml`, `runner.toml`, and `.env.example` with commented-out, ready-to-edit templates. A new user should be able to read each generated file top-to-bottom, understand what each field means, uncomment the lines they need, fill in values, and have a working config — without ever reading Carve source.

This is purely UX polish. The loader behavior is unchanged: every template is fully commented, so a freshly-initialized project still resolves to schema defaults plus whatever the user uncomments.

## Scope

### In scope

- New body for `carve/connections.toml` covering the Snowflake target plus the three auth methods (password, key-pair, externalbrowser) as alternatives.
- New body for `carve/models.toml` showing both auth modes (`api_key` today, `claude_code_oauth` once M1.1-02 lands — referenced as a forward pointer for now).
- New body for `carve/runner.toml` showing every `RunnerConfig` field with a short comment.
- Expanded `.env.example` listing every env var referenced by the templated configs.
- Test updates: assert anchor strings in each generated file rather than exact contents (so the templates can evolve without churning the test).
- **Loader relaxation**: today's `ModelsConfig.anthropic_api_key: str` (required, no default) blocks `load_config()` against a fully-commented `models.toml`. Make the field optional at load time (`anthropic_api_key: str | None = None`) so commands that don't need the API key (`init`, `runs`, `logs`, `version`) still work; commands that do (`plan`, `build`) raise a clear ConfigError at use time. The relaxation is the precondition that lets `carve init`'s commented templates produce a config the loader accepts.

### Out of scope

- Schema-driven generation from the Pydantic models. Hand-written templates are simpler to read and easier to keep helpful.
- Interactive prompts during `carve init`.
- Documenting every field exhaustively. One short comment per field is enough; long-form docs go in the README.
- Changes to the loader, the schema, or any other module outside `init.py` and the matching tests.

## Generated content

The exact wording can be tuned during implementation; the shape and coverage below is what the spec mandates.

### `carve/connections.toml`

```toml
# Connection definitions for Snowflake (and future connectors).
# The key after `[snowflake.<target>]` is the target name, referenced from
# carve.toml's `default_target` (default: "dev").
#
# Use ${VAR_NAME} to interpolate environment variables from .env or your shell.

# [snowflake.dev]
# account = "${SNOWFLAKE_ACCOUNT}"          # e.g. "abc12345.us-east-1"
# user = "${SNOWFLAKE_USER}"
# password = "${SNOWFLAKE_PASSWORD}"
# role = "${SNOWFLAKE_ROLE}"                # e.g. "SYSADMIN"
# warehouse = "${SNOWFLAKE_WAREHOUSE}"      # e.g. "COMPUTE_WH"
# database = "${SNOWFLAKE_DATABASE}"
# schema = "PUBLIC"                          # optional; defaults to PUBLIC

# Alternative auth methods (uncomment one and remove `password = ...`):
#
# Key-pair:
#   private_key_path = "/path/to/rsa_key.p8"
#   # set SNOWFLAKE_PRIVATE_KEY_PASSPHRASE in your env if the key is encrypted
#
# SSO / external browser (dev only — pops a browser window):
#   authenticator = "externalbrowser"
```

### `carve/models.toml`

The whole file *is* the `[models]` section — do **not** add a `[models]` or `[anthropic]` header. Fields go at the top level.

> **Updated during implementation (2026-04-29):** the commented `default_model` value uses the pinned model id `claude-sonnet-4-5-20250929` (matching the schema default) instead of the floating alias `claude-sonnet-4-5`, so a user who uncomments the line verbatim hits a real billable model rather than the $0-cost alias.

```toml
# Anthropic / model configuration. The keys here populate the `models`
# section of the merged config — write fields at the top level, no header.

# anthropic_api_key = "${ANTHROPIC_API_KEY}"
# default_model = "claude-sonnet-4-5-20250929"

# To use your Claude Code subscription instead of an API key, see M1.1-02
# (auth_mode = "claude_code_oauth"). Not yet implemented as of this version.
```

### `carve/runner.toml`

The whole file *is* the `[runner]` section — do **not** add a `[runner]` header.

```toml
# Runner configuration. The keys here populate the `runner` section of
# the merged config — write fields at the top level, no header.
# The `local_venv` runner is the only M1 option; Docker / remote runners
# arrive later.

# type = "local_venv"
# venv_cache_dir = ".carve/venvs"
# default_timeout_seconds = 1800
# max_concurrent_runs = 4
```

### `.env.example`

```
# Copy this to `.env` and fill in real values. `.env` is gitignored.
# ANTHROPIC_API_KEY=

# Snowflake (used by carve/connections.toml's [snowflake.dev]):
# SNOWFLAKE_ACCOUNT=
# SNOWFLAKE_USER=
# SNOWFLAKE_PASSWORD=
# SNOWFLAKE_ROLE=
# SNOWFLAKE_WAREHOUSE=
# SNOWFLAKE_DATABASE=
# SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=
```

## Implementation

### `src/carve/cli/commands/init.py`

- Replace `CONNECTIONS_TOML_CONTENT`, `RUNNER_TOML_CONTENT`, `MODELS_TOML_CONTENT`, and `ENV_EXAMPLE_CONTENT` constants with the bodies above.
- Keep the `_write_if_missing` / `_ensure_dir` helpers unchanged.
- Don't touch `carve.toml`'s body — it stays uncommented, since the loader requires it to be present for a project to be valid.

### `src/carve/core/config/schema.py`

- Change `ModelsConfig.anthropic_api_key: str` to `anthropic_api_key: str | None = None`.
- Update the field's docstring/comment to record that the value is required at use-time (plan/build), not load-time.

> **Updated during implementation (2026-04-29):** `init._initialize_state_store` synthesises a Config solely to drive the state-store helpers and currently passes a hardcoded `anthropic_api_key="bootstrap"` rather than `None`. That path never reads the field, so functionally it's a no-op, but it should switch to `None` once any field-format validation (e.g. an `sk-ant-` prefix check) is added to `ModelsConfig` — flagged in the implementation security review.

### `src/carve/cli/orchestrator/planner.py` (and any other use site)

- At the point where `config.models.anthropic_api_key` is consumed (today: building the Anthropic client), assert it's not `None` and raise `ConfigError` with file/field/hint pointing the user at `carve/models.toml`. The error wording mirrors the existing M1-02 ConfigError shape so the CLI's existing exit-code-2 path catches it.

No other modules touch the API key field today; double-check via `rg anthropic_api_key src/` during implementation.

## Tests

Update `tests/test_cli.py`:

- `test_init_creates_expected_layout` — already asserts the file paths exist; keep as-is.
- `test_init_carve_toml_content` — unchanged.
- Replace / expand `test_init_writes_models_toml_placeholder` with **anchor-string** assertions for each templated file:
  - `connections.toml` contains `# [snowflake.dev]` and `# account = "${SNOWFLAKE_ACCOUNT}"` and `# authenticator = "externalbrowser"`.
  - `models.toml` contains `# anthropic_api_key = ` and `# default_model = "claude-sonnet-4-5-20250929"` (pinned id, matching the schema default). **Must not** contain a `[models]` or `[anthropic]` header — assert their absence.
  - `runner.toml` contains `# type = "local_venv"` and `# default_timeout_seconds = 1800`. **Must not** contain a `[runner]` header — assert its absence.
  - `.env.example` contains `# SNOWFLAKE_ACCOUNT=` and `# SNOWFLAKE_USER=`.
- Add an integration-shaped test (no real network) that calls `load_config()` against a `tmp_path` initialized by `carve init`. The loader should succeed using schema defaults (no real values uncommented), proving the templated files still parse to a valid empty-shaped config.

> **Updated during implementation (2026-04-29):** `tests/core/config/test_loader.py::test_missing_required_field_has_helpful_message` was repointed away from `models.anthropic_api_key` (now optional after the loader relaxation in this spec, so it no longer triggers a "missing required field" error) onto `project.name`, which is still required. The intent of the test — proving that a missing required field surfaces a helpful error — is preserved; only the field under test changed.

`tests/core/config/test_loader.py` — one test (`test_missing_required_field_has_helpful_message`) and its fixture were repointed from `models.anthropic_api_key` to `project.name`. The rest of the file is unchanged; the fixtures don't depend on `carve init`'s output.

## Acceptance criteria

- `carve init` in a fresh tmpdir produces config files that a non-author can fill in without reading Carve source.
- Anchor-string tests pass.
- `load_config()` succeeds against the freshly-initialized project (no errors, all defaults).
- `ruff` + `mypy --strict` + the full `pytest` suite stay green.
- A short `## [Unreleased]` note in `CHANGELOG.md` documents the change.

## Files this spec produces

> **Updated during implementation (2026-04-29):** `tests/core/config/test_loader.py` and `tests/core/config/fixtures/missing_required/carve.toml` were added to the modified list — the loader relaxation made `models.anthropic_api_key` optional, so the existing "missing required field" test had to be repointed at `project.name` and the fixture had to drop `name` instead of `models.toml`.

Modified:

- `src/carve/cli/commands/init.py` (templates)
- `src/carve/core/config/schema.py` (`anthropic_api_key` becomes optional)
- `src/carve/cli/orchestrator/planner.py` (raise ConfigError at use-time when key is None)
- `tests/test_cli.py`
- `tests/core/config/test_schema.py` (validate the new field default)
- `tests/core/config/test_loader.py` (repoint missing-required test from `models.anthropic_api_key` to `project.name`)
- `tests/core/config/fixtures/missing_required/carve.toml` (omit `project.name` instead of relying on missing `models.toml`)
- `tests/cli/orchestrator/test_planner.py` (assert ConfigError when key is None at plan time)
- `CHANGELOG.md`

No new files.

## What this enables

- A new user can finish the M1 acceptance flow without anyone holding their hand through the connection schema.
- M1.1-02 has a natural place to advertise the OAuth auth mode (the `models.toml` template).
- The README walkthrough can stop showing TOML snippets inline — it can just say "edit the generated `connections.toml`".
