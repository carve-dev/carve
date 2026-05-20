# v0.1-01-followup — M1 test fixture sweep + missing v0.1-01 unit tests

> Closes the deferred work from [`v0.1-01`](./01-state-store-postgres.md). The Postgres-from-day-one shift broke ~200 M1-era tests that built their own SQLite-backed `Config` or invoked `carve init` via `CliRunner` without a reachable Postgres. This spec sweeps them all and adds three unit tests that v0.1-01 ## Tests required but didn't ship.

## Status

- **Status:** Drafting
- **Depends on:** [v0.1-01 state-store-postgres](./01-state-store-postgres.md) — uses the Postgres testcontainers fixture that v0.1-01 added to `tests/conftest.py`
- **Blocks:** nothing structurally — but it gates the v0.1 regression bar ("the full M1 test suite passes against Postgres" per v0.1-01 ## Acceptance). v0.1-02 onward can be developed in parallel; they just need this spec landed before v0.1.0 ships.

## Goal

Bring the M1 test suite back to green against the v0.1-01 Postgres state store, in a single sweep. After this spec lands:

- `uv run pytest tests/ -q` is green
- The three v0.1-01 ## Tests bullets that have no coverage are covered
- `DEFAULT_STATE_STORE_URL = "postgresql+psycopg://carve:carve@localhost:5432/carve"` is documented as dev-only (security-reviewer Low finding from v0.1-01)

This spec is **test + doc work only**. No new functionality. No production code changes outside the one docstring touch-up.

> **Updated during implementation (2026-05-20):** A second small production change landed in `src/carve/cli/commands/init.py` (user-authorized scope expansion during reviewer cleanup) — the misleading `+ .carve/state.db` print line and its surrounding docstring were updated to reflect that init now bootstraps a Postgres schema rather than a SQLite file. Still "no new functionality", but the "outside the one docstring touch-up" wording no longer holds literally. See the §Files-this-spec-produces callout and §Acceptance for the full picture.

## Out of scope

- Any new feature, refactor, or runtime behavior change. v0.1-01 already landed the runtime correctness; this spec is the test sweep that comes after.
- Migrating tests that don't actually need the state store. Some `_make_config` helpers exist in tests that never touch the engine (e.g., `tests/core/targets/test_resolution.py`, `tests/core/connectors/test_snowflake.py`); leave those alone unless they're actively broken.
- Performance tuning of the testcontainers Postgres fixture. v0.1-01 already used a session-scoped container with per-test database — that pattern is fine as-is.

## Files this spec produces

> **Updated during implementation (2026-05-20):** Added two files that were touched during the sweep but missing from the original list — `tests/conftest.py` (the `cli_env` fixture the spec's Bucket C narrative calls for) and `tests/core/config/fixtures/valid_full/carve/server.toml` (one-line flip of the dev URL from `sqlite:///...` to `postgresql+psycopg://...` so the spec's own "zero `sqlite:///` in tests/" acceptance bullet holds).

```
tests/migrations/test_migrations.py                     # MODIFY — Bucket B rewrite
tests/core/runners/test_local_venv.py                   # MODIFY — Bucket A fixture swap
tests/integration/test_extract_load_flow.py             # MODIFY — Bucket A fixture swap
tests/cli/commands/target/test_create.py                # MODIFY — Bucket C (env override for CliRunner)
tests/cli/commands/target/test_delete.py                # MODIFY — same
tests/cli/commands/target/test_list.py                  # MODIFY — same
tests/cli/commands/target/test_rename.py                # MODIFY — same
tests/cli/commands/target/test_show.py                  # MODIFY — same
tests/cli/commands/test_init_centralized.py             # MODIFY — Bucket C + semantic review
tests/cli/commands/el/test_list.py                      # MODIFY — Bucket C
tests/cli/commands/el/test_run.py                       # MODIFY — Bucket C
tests/cli/commands/el/test_verify.py                    # MODIFY — Bucket C
tests/cli/commands/el/test_deploy.py                    # MODIFY — fixture review (partial sweep in 23bcf88)
tests/cli/orchestrator/test_builder.py                  # MODIFY — Bucket C
tests/cli/orchestrator/test_planner.py                  # MODIFY — Bucket C
tests/cli/orchestrator/test_runner.py                   # MODIFY — Bucket C
tests/cli/orchestrator/test_recovery.py                 # MODIFY — Bucket C tail (partial sweep in 23bcf88)
tests/core/agents/test_extract_load_agent.py            # MODIFY — Bucket A tail (~30 test methods)
tests/core/skills/test_plan_agent_integration.py        # MODIFY — Bucket A/C
tests/core/deploy/test_preflight.py                     # MODIFY — Bucket A
tests/core/state/test_database.py                       # MODIFY — Bucket B rewrite + new unit tests (bullets 1, 2)
tests/core/config/test_schema.py                        # MODIFY — Bucket B rewrite
tests/test_cli.py                                       # MODIFY — top-level CLI smoke tests; Bucket C
tests/conftest.py                                       # MODIFY — add the `cli_env` fixture (Bucket C support)
tests/core/config/fixtures/valid_full/carve/server.toml # MODIFY — flip dev URL from sqlite:// to postgresql+psycopg:// so the SQLite-retirement grep stays clean
src/carve/core/config/state_store.py                    # MODIFY — docstring: DEFAULT_STATE_STORE_URL labeled dev-only
src/carve/cli/commands/init.py                          # MODIFY — see callout below: docstring + post-init print line
docs/installation.md                                    # MODIFY — explicit "default credentials are dev-only" callout
```

> **Updated during implementation (2026-05-20):** `src/carve/cli/commands/init.py` was added to this list mid-implementation. The original spec scoped production-code changes to a single docstring touch-up in `src/carve/core/config/state_store.py`; reviewer cleanup surfaced that `_initialize_state_store` still printed `+ .carve/state.db` after the SQLite-retirement, which contradicted what the function actually does. The user explicitly authorized expanding scope to fix the misleading print line and its docstring (now: `+ state store schema initialized (postgres)`). This is a strategic scope expansion beyond the "test + doc work only" guardrail and is also reflected in the §Acceptance callout below and in the existing major-drift proposal.

## Behavior

### Bucket A — fixture swap

Tests in this bucket build a `Config` with `ServerConfig(state_store="sqlite:///...")` and never run `carve init` via CliRunner. The fix is mechanical:

1. Add `postgres_state_store_url: str` to the fixture or test function signature
2. Replace the SQLite URL string with the fixture value: `ServerConfig(state_store=postgres_state_store_url)`
3. Where `_config()` is a helper, accept the URL as a parameter

Files: `tests/core/runners/test_local_venv.py`, `tests/integration/test_extract_load_flow.py`, `tests/core/agents/test_extract_load_agent.py` (signature already changed in `23bcf88`; thread the URL into ~30 test methods), `tests/core/skills/test_plan_agent_integration.py`, `tests/core/deploy/test_preflight.py`.

### Bucket B — modest rewrites

Tests in this bucket assert directly against the SQLite engine, schema reflection, or migration behavior. The Postgres equivalent works differently in places (JSONB vs TEXT, TIMESTAMPTZ vs TEXT timestamps, partial unique indexes on jobs not yet present in v0.1-01's schema). Each needs targeted review:

- **`tests/migrations/test_migrations.py`** — the existing tests assume Alembic upgrades a clean SQLite. Switch the engine to the per-test Postgres database via `postgres_state_store_url`. Assertions about table types (TEXT vs JSONB) and timestamp columns (NOT NULL DEFAULT now() vs nullable naive) flip to the Postgres expectation. Bullet 4 of v0.1-01 ## Tests ("`alembic upgrade head` on empty Postgres → schema matches expected DDL") lands a new test here.

  > **Updated during implementation (2026-05-20):** "Rewrite" was the right framing — the SQLite-only legacy-migration tests (the SQLite→Postgres migrator was already removed in `2a87d7e`) had no Postgres equivalent and were dropped rather than ported. The retained tests are the per-revision Alembic walk against Postgres (`0003` rename, `0004` builds + FK rewire, `0005` runs.target, `0006` recovery chains) plus the new `test_alembic_upgrade_head_on_empty_postgres`.
- **`tests/core/state/test_database.py`** — exercises `create_engine_from_config`, `initialize_database`, and connection-lifecycle assertions. Rewrite against Postgres. Bullets 1 and 2 of v0.1-01 ## Tests land here as new test functions:
  - `test_model_metadata_reflects_against_postgres` — open a Postgres engine, `inspect(engine).get_table_names()` returns the expected M1-shape tables (plus whatever's already added in v0.1-01)
  - `test_engine_factory_rejects_non_postgres_url` — `pytest.raises(StateStoreBackendError, match="postgresql\\+psycopg://")` for `sqlite:///`, `mysql://`, and a malformed URL
- **`tests/core/config/test_schema.py`** — exercises the Pydantic schema validation for the new `StateStoreConfig` and the backward-compat `ServerConfig.state_store` alias. Make sure tests cover env-var interpolation, the alias falling through to `state_store.url` at load time, and rejection of fields not in `model_config = ConfigDict(extra="forbid")`.

### Bucket C — CliRunner env override

Tests in this bucket invoke `carve init` (or other CLI commands that need a reachable Postgres) via `typer.testing.CliRunner.invoke(...)`. The current failure mode is `psycopg.OperationalError: connection refused` because `carve init` runs `initialize_database` at the end of its flow.

Two options for each affected test:

**Option C1 (preferred when the test exercises post-init behavior):**

- Use the `postgres_state_store_url` fixture
- Pass `env={"DATABASE_URL": postgres_state_store_url}` to `CliRunner.invoke(...)` so the spawned init resolves to the test's Postgres database

**Option C2 (when the test only checks init's file-writes, not the post-init state):**

- Add a `--skip-postgres-bootstrap` flag to `carve init` that lands the files but defers `initialize_database` to the first `carve serve` call
- Note: this requires a small production-code change. Out of scope for this spec unless the test cost of doing C1 is unreasonable; default is C1.

A helper in `tests/conftest.py` is worth adding to avoid boilerplate:

```python
@pytest.fixture
def cli_env(postgres_state_store_url: str) -> dict[str, str]:
    """Env dict for CliRunner.invoke; routes the spawned process at the per-test Postgres."""
    return {"DATABASE_URL": postgres_state_store_url}
```

Test sites then call `runner.invoke(app, [...], env=cli_env)` consistently.

Files: `tests/cli/commands/target/test_create.py`, `test_delete.py`, `test_list.py`, `test_rename.py`, `test_show.py`, `tests/cli/commands/test_init_centralized.py`, `tests/cli/commands/el/test_list.py`, `test_run.py`, `test_verify.py`, `test_deploy.py` (tail of partial sweep), `tests/cli/orchestrator/test_builder.py`, `test_planner.py`, `test_runner.py`, `test_recovery.py` (tail of partial sweep), `tests/test_cli.py`.

### Bucket C — `test_init_centralized.py` semantic review

This is the one Bucket C file that needs more than env-override. `carve init`'s post-v0.1-01 behavior added new scaffolded files (state_store config in runtime.toml, the Postgres `DATABASE_URL` placeholder in `.env.example`, etc.). The existing assertions about init's output need to be reviewed:

- Anything asserting on file contents (e.g., "the `.env.example` looks like X") needs to be updated to the v0.1 shape
- Anything asserting on Config validity after init needs to use the cli_env fixture
- New assertions worth adding: `state_store` key is present in `carve.toml` or `runtime.toml` per v0.1-01's config schema

### New unit tests (v0.1-01 ## Tests gap)

The three missing bullets:

1. **Model metadata reflection on Postgres** (bullet 1): in `tests/core/state/test_database.py`, a `test_model_metadata_reflects_against_postgres` that opens an engine via the postgres fixture, calls `inspect(engine).get_table_names()`, and asserts the expected M1-shape tables (`runs`, `logs`, `plans`, `pipelines`, `builds`) are present with the right column types where v0.1-01 changed them (JSONB on `Plan.task_graph_json` / `Build.manifest_json`; TIMESTAMPTZ on the columns v0.1-01 updated).

2. **Engine factory rejection of non-Postgres URLs** (bullet 2): in `tests/core/state/test_database.py`, a `test_engine_factory_rejects_non_postgres_url` parameterized with `sqlite:///path`, `mysql://user@host/db`, and `not-a-url`. Each raises `StateStoreBackendError` with a `match=` regex pointing at `docs/installation.md`.

3. **Alembic upgrade head on empty Postgres** (bullet 4 from v0.1-01 ## Tests): in `tests/migrations/test_migrations.py`, a `test_alembic_upgrade_head_on_empty_postgres` that creates a fresh database, runs `alembic upgrade head`, and asserts the resulting schema's table list and selected column types match expected. This is the schema-shape regression test.

### Documentation: dev-only label on default credentials

`src/carve/core/config/state_store.py` — update the `DEFAULT_STATE_STORE_URL` docstring or surrounding module docstring to say (paraphrasing):

> `DEFAULT_STATE_STORE_URL` matches the bundled docker-compose default credentials. It is **only suitable for local development**. Production installs must override via `DATABASE_URL` env var or `state_store.url` in `runtime.toml`. The hosted product never uses this default — its control plane injects a managed connection string.

`docs/installation.md` — add a short callout under "First-run with bundled Postgres":

> ⚠️ The bundled `docker-compose.yml` uses default credentials (`carve` / `carve`) suitable for local development only. Do not expose this Postgres to the network; for any production-shaped install, use `--external-postgres` with credentials you control.

## Tests

This spec is itself test work; the deliverables ARE tests. The acceptance bar is the full pytest sweep.

- `uv run pytest tests/ -q` → all green
- `grep StateStoreBackendError tests/` → at least the three new unit tests from §New unit tests
- `grep -rn "sqlite:///" tests/ | grep -v _archive` → returns nothing (no SQLite URLs in the active test tree)

## Acceptance

- The full v0.1-era test suite passes against Postgres: `uv run pytest tests/ -q` → 0 failed, 0 errors
- The three v0.1-01 ## Tests bullets that had no coverage now have dedicated tests
- `src/carve/core/config/state_store.py` and `docs/installation.md` document the dev-only nature of the default credentials
- No production code changes outside the docstring updates (this is a test sweep)
  > **Updated during implementation (2026-05-20):** Held with one user-authorized scope expansion. `src/carve/cli/commands/init.py::_initialize_state_store` had its docstring updated *and* its post-init `console.print` line replaced (`{project_root}/.carve/state.db` → `state store schema initialized (postgres)`) so the user-visible output no longer references the retired SQLite file. The original spec wording is preserved as the intent; the print-line change is recorded here so future readers know this acceptance bullet was relaxed deliberately, not violated silently.
- A reviewer can `grep "sqlite:///" tests/` and find zero hits (the SQLite source pattern is fully retired from the test surface)

## Design notes

- **Why not auto-stub Postgres in the test environment?** Some tests genuinely exercise behavior that needs a real database (Alembic migrations, JSONB round-tripping, partial-unique-index enforcement). A stub Postgres (e.g., in-memory SQLite wearing a Postgres dialect mask) wouldn't surface the real shapes. testcontainers gives us a real Postgres at ~5s session-startup cost, which is acceptable.
- **Why `cli_env` as a fixture rather than a per-test parameter?** Because nearly every CliRunner-invoking test needs the same env shape, and a shared fixture centralizes the convention. Also makes future env additions (e.g., a `CARVE_PROJECT_DIR` override) one-line changes.
- **Why is `test_init_centralized.py` called out separately?** Its assertions test the *shape* of what init writes, not just that init succeeds. v0.1-01 changed that shape. So it needs review beyond the mechanical env override.
- **Why ship the dev-only docstring callout in this spec rather than v0.1-01?** Because v0.1-01 was already big enough and the credential default landed alongside the bundled-Postgres path (which is v0.1-02). Catching it in this followup keeps v0.1-01 focused on what it did and clusters the credential-default discussion with the test sweep where it's small.

## Open questions

- **Should we add `--skip-postgres-bootstrap` to `carve init`?** *Implementation default.* No in this spec; let tests use C1 (env-override). Add the flag in a future spec if the post-init-no-Postgres workflow becomes important for offline-CI scenarios. If a test really can't use C1, the engineer can flag it.
- **Are there any `_make_config` helpers I should refactor into a shared util?** *Implementation default.* No. Each test file's `_make_config` is small and local; consolidating creates an indirection that's worse than the slight duplication.
- **Should the new `test_engine_factory_rejects_non_postgres_url` test be parameterized over additional URL shapes (mssql, oracle, postgresql+asyncpg)?** *Implementation default.* Test the three obvious ones (sqlite, mysql, malformed). Adding more is low value; the rejection logic is a single substring check, not per-dialect.
- **Should we also exercise `state_store.url` env-var interpolation in `test_schema.py`?** *Implementation default.* Yes if it doesn't already; v0.1-01's `state_store.py` does the interpolation and v0.1-01's tests probably already cover it but verify during implementation.
