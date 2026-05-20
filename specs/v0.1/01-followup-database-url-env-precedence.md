# v0.1-01c — Native `DATABASE_URL` precedence in `resolve_state_store_url`

> Small production-code followup to [`v0.1-01-followup-m1-test-sweep`](./01-followup-m1-test-sweep.md). Closes the production gap that forced the test sweep to ship a `cli_env` monkeypatch shim. When this lands, the shim collapses to a one-line dict.

## Status

- **Status:** Landed (2026-05-20)
- **Depends on:** [v0.1-01 state-store-postgres](./01-state-store-postgres.md) (modifies the resolver added there)
- **Blocks:** nothing — but is a precondition for cleanly removing the `cli_env` monkeypatch in [`v0.1-01-followup-m1-test-sweep`](./01-followup-m1-test-sweep.md). With this landed, the parent spec's Caveat 1 is resolved (see callout in §Acceptance).

## Goal

Teach `resolve_state_store_url` to read `DATABASE_URL` from the environment as a first-class override source, so the user-visible promise made by the source docstring and the `_NON_POSTGRES_MESSAGE` error text actually holds in code.

Concretely, after this spec lands:

- A user with `export DATABASE_URL=postgresql+psycopg://...` in their shell gets that URL routed to the engine factory during `carve init`, `carve serve`, and every other CLI entry, without needing a `runtime.toml` to exist yet.
- The `cli_env` fixture in `tests/conftest.py` collapses from "env dict + function-scoped monkeypatch on `database_mod.resolve_state_store_url`" to just `return {"DATABASE_URL": postgres_state_store_url}` — matching what the parent spec originally claimed Bucket C would look like.
- The new precedence is explicit and testable: `state_store.url` (when not default) > `DATABASE_URL` env > `server.state_store` legacy alias (when not default) > `DEFAULT_STATE_STORE_URL`.

This spec is **production code + tests**. No new user-facing CLI surface; no new config keys; no schema migration. The only public-API change is one function's behavior, in a way that the existing docstring already implies.

## Out of scope

- Adding any other env var to the resolver (e.g., `CARVE_DATABASE_URL`, per-target env vars). One env, the canonical one Postgres tooling uses.
- Touching the generic `${VAR}` interpolation in `src/carve/core/config/loader.py`. That interpolation runs on parsed TOML and is unrelated to this gap — the bootstrap Config built by `carve init` doesn't go through the loader.
- Changing pool sizing, dialect rejection, or any other resolver-adjacent behavior. Keep this surgical.
- Removing the `cli_env` shim's monkeypatch immediately. That happens in a single follow-up commit (also part of this spec — see ## Behavior), but the broader `cli_env` fixture and its env-dict role stay.

## Files this spec produces

> **Updated during implementation (2026-05-20):** Added `tests/core/state/test_database.py` to the list. The engineer added one new test (`test_database_url_env_with_non_postgres_dialect_is_rejected`) there to close the test gap explicitly invited by §Open questions item 3 ("worth one test case that confirms `DATABASE_URL=sqlite:///bad.db` produces the same `StateStoreBackendError`"). Pure additive; no other changes to that file.

```
src/carve/core/config/state_store.py            # MODIFY — resolve_state_store_url reads DATABASE_URL
tests/core/config/test_state_store.py           # NEW — three unit tests for precedence
tests/conftest.py                               # MODIFY — collapse cli_env's monkeypatch to a no-op
tests/core/state/test_database.py               # MODIFY — add env-sourced dialect-rejection test (per §Open questions #3)
```

Four files.

## Behavior

### Production change — `resolve_state_store_url`

Current code (`src/carve/core/config/state_store.py:56-73`):

```python
def resolve_state_store_url(config: Config) -> str:
    if config.state_store.url != DEFAULT_STATE_STORE_URL:
        return config.state_store.url
    if config.server.state_store and config.server.state_store != DEFAULT_STATE_STORE_URL:
        return config.server.state_store
    return config.state_store.url
```

Replace with:

```python
def resolve_state_store_url(config: Config) -> str:
    """Resolve the effective state-store URL from a loaded config.

    Precedence (highest to lowest):
    1. ``state_store.url`` from ``runtime.toml`` — an explicit, non-default
       value wins over any env var.
    2. ``DATABASE_URL`` env var — the canonical Postgres env var. Honored
       even when no ``runtime.toml`` has been written yet (the
       ``carve init`` bootstrap case).
    3. ``server.state_store`` from the legacy ``server.toml`` — kept for
       M1 in-tree projects that haven't migrated yet. Removed in v0.2.
    4. The module default (``DEFAULT_STATE_STORE_URL``).
    """
    if config.state_store.url != DEFAULT_STATE_STORE_URL:
        return config.state_store.url
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    if config.server.state_store and config.server.state_store != DEFAULT_STATE_STORE_URL:
        return config.server.state_store
    return config.state_store.url
```

Add `import os` at the top of the module if it isn't already there (it is not as of `4429000`).

The module docstring and the `DEFAULT_STATE_STORE_URL` docstring already claim this behavior in prose. This spec makes them true in code.

### Test sweep — `tests/core/config/test_state_store.py` (NEW)

A new file, three tests, no fixtures beyond what's already in `tests/conftest.py`:

1. **`test_explicit_state_store_url_wins_over_database_url_env`** — set `DATABASE_URL` to a sentinel value via `monkeypatch.setenv`, build a `Config` whose `state_store.url` is set to a *different* concrete URL (not the default), assert `resolve_state_store_url(config)` returns the config URL, not the env URL.

2. **`test_database_url_env_wins_when_state_store_url_is_default`** — set `DATABASE_URL` to a sentinel, build a `Config` whose `state_store.url` is `DEFAULT_STATE_STORE_URL` (which is what `carve init`'s bootstrap Config produces), assert `resolve_state_store_url(config)` returns the env URL. This is the test that proves the production gap is closed.

3. **`test_falls_through_to_legacy_then_default`** — parameterize over four cases:
   - No env, no legacy → default
   - No env, legacy set → legacy
   - Env set, legacy also set, `state_store.url` is default → env (env beats legacy)
   - Env unset, legacy unset, `state_store.url` is non-default → state_store.url

Each case uses `monkeypatch.delenv("DATABASE_URL", raising=False)` to make env state explicit.

### Test cleanup — `tests/conftest.py::cli_env`

Once the production resolver honors `DATABASE_URL` natively, the monkeypatch in `cli_env` is dead weight. Collapse the fixture to:

```python
@pytest.fixture
def cli_env(postgres_state_store_url: str) -> dict[str, str]:
    """Env dict for CliRunner.invoke; routes the spawned process at the per-test Postgres."""
    return {"DATABASE_URL": postgres_state_store_url}
```

The `monkeypatch` parameter is gone, the `_resolve_with_test_fallback` closure is gone, the imports of `DEFAULT_STATE_STORE_URL` / `resolve_state_store_url` / `database_mod` are gone, and the long docstring trims to two lines.

Verify the full pytest sweep stays green after the collapse: `uv run pytest tests/ -q` → 756+ passed (3 skipped expected).

## Tests

- New tests in `tests/core/config/test_state_store.py` (the three above) all pass.
- Existing tests still pass without the shim: `uv run pytest tests/ -q` → 0 failed, 0 errors.
- `ruff check` and `mypy src/ tests/` stay clean.

## Acceptance

- `resolve_state_store_url` consults `DATABASE_URL` env per the precedence table above. The behavior is exercised by three named tests in `tests/core/config/test_state_store.py`.
- The `cli_env` fixture in `tests/conftest.py` is reduced to a one-line env dict — no monkeypatch, no imports beyond `pytest` and the URL fixture.
- The full test sweep stays green against Postgres.
- The parent spec's Caveat 1 (under §Acceptance in `01-followup-m1-test-sweep.md`) becomes obsolete and can be removed in a spec-keeper pass after this lands.

## Design notes

- **Why `os.environ.get` and not the generic `${VAR}` loader?** The loader interpolates TOML at parse time. `carve init`'s bootstrap Config is constructed in Python without ever loading TOML — so the loader's interpolation pass is bypassed entirely. The resolver is the right level to consult the env because it's the single chokepoint every state-store-touching code path runs through.
- **Why does an explicit non-default `state_store.url` win over `DATABASE_URL`?** Because the user wrote it down. Env vars are convenient for "I don't have a config file yet" and for hosted-product control-plane injection. A committed `state_store.url` is intentional configuration — it should not be silently overridden by an env var.
- **Why isn't this just baked into v0.1-01?** Because v0.1-01 was already large and shipping. The gap surfaced only when the test sweep tried to use the env-var path against `carve init`'s bootstrap. Fixing it here keeps each spec focused.
- **Could we drop the legacy `server.state_store` step?** Not yet. v0.1-01's `01-followup-m1-test-sweep` still has `_make_config(state_db=...)` helpers in ~15 test files that construct `Config(server=ServerConfig(state_store=...))`. A separate v0.2 cleanup removes those helpers and the legacy alias together.

## Open questions

- **Should we also accept `CARVE_DATABASE_URL` as a synonym?** *Implementation default.* No. `DATABASE_URL` is the cross-tool standard. Adding a Carve-specific name fragments the convention without solving any problem.
- **Should `DATABASE_URL` empty-string (`""`) be treated as unset?** *Implementation default.* Yes. Treat empty string the same as missing — `os.environ.get("DATABASE_URL")` returning `""` should fall through to the legacy/default branches. One extra `if env_url:` truthiness check covers this; spell it out as `if env_url:` (not `if env_url is not None:`) in the code.
- **Should the rejection check on non-Postgres URLs (`StateStoreBackendError`) also run against `DATABASE_URL` values?** *Implementation default.* Yes, transparently. The rejection lives in `create_engine_from_config` / `initialize_database` and runs on whatever URL `resolve_state_store_url` returns, so this comes for free — no extra code, but worth one test case that confirms `DATABASE_URL=sqlite:///bad.db` produces the same `StateStoreBackendError` as the equivalent `state_store.url` setting.
  > **Resolved during implementation (2026-05-20):** Test landed as `test_database_url_env_with_non_postgres_dialect_is_rejected` in `tests/core/state/test_database.py`. Sets `DATABASE_URL=sqlite:///bad.db`, builds a Config with default `state_store.url`, asserts `create_engine_from_config(config)` raises `StateStoreBackendError` with `match=r"postgresql\+psycopg://"` — same shape as the existing `test_engine_factory_rejects_non_postgres_url` parameterized test.
