# Spec update proposal: v0.1-01-followup

**Generated:** 2026-05-20 (updated 2026-05-20 after reviewer-suggestion cleanup landed)
**Source spec:** `specs/v0.1/01-followup-m1-test-sweep.md`
**Reason:** Two pieces of substantive drift this proposal records.

**(1) Production-gap shim in `cli_env`.** Implementing Bucket C surfaced a production gap that the spec did not anticipate. The spec describes Bucket C as a mechanical env-override (`runner.invoke(app, [...], env={"DATABASE_URL": ...})`). That works only when production `resolve_state_store_url` honors `DATABASE_URL` as an env-var override during `carve init`. Today it does not — it only sees `DATABASE_URL` if `runtime.toml` already contains `url = "${DATABASE_URL}"` for interpolation, and during `carve init` there is no `runtime.toml` yet, so the bootstrap Config falls through to `DEFAULT_STATE_STORE_URL` unconditionally. The engineer worked around this with a function-scoped monkeypatch in the `cli_env` fixture (see `tests/conftest.py:126-177`), which the QA and security reviewers both confirmed is correctly scoped but is documenting a real production gap in test-only code. The followup that closes the gap is not yet planned anywhere.

The drift is substantive because it changes what "Bucket C — CliRunner env override" *means* in practice: the spec described it as a configuration trick, but the actual implementation required a test-side shim over a production behavior the spec assumed already existed. Future readers of the spec will see "just pass `env={"DATABASE_URL": ...}`" and be misled about what production does today.

**(2) User-authorized scope expansion into `src/carve/cli/commands/init.py`.** The spec's §Acceptance bullet "No production code changes outside the docstring updates" was relaxed during reviewer-suggestion cleanup: `_initialize_state_store` had its post-init `console.print` line replaced (`{project_root}/.carve/state.db` → `state store schema initialized (postgres)`) alongside its docstring update, because the original print line was a SQLite-retirement straggler that contradicted what the function actually does. The user explicitly authorized this as part of the "address all 5 reviewer suggestions" instruction. This is small in absolute size (one print line + one docstring) but a real expansion of the spec's stated guardrail, so it warrants a §Files-this-spec-produces entry and a §Acceptance callout — both already applied inline in the spec — plus discussion below.

**Post-cleanup shim shape (2026-05-20 update).** Two cleanups have shrunk the `cli_env` shim since this proposal was first written:

- The fixture no longer double-patches: it patches `database_mod.resolve_state_store_url` only (the dead patch on `state_store_mod.resolve_state_store_url` was removed because nothing in the runtime call graph re-imports through that module reference). The proposal sections below have been updated to reflect this cleaner shape.
- The inner `_resolve_with_test_fallback` now closes over `postgres_state_store_url` directly instead of reading `os.environ["DATABASE_URL"]` at call time. Functionally identical inside `CliRunner.invoke` (where `DATABASE_URL` is the per-test URL), more robust outside it (a developer's shell `DATABASE_URL` can't leak into post-invoke direct calls).

## Affected sections

### Bucket C — CliRunner env override

**Current spec text:**

> **Option C1 (preferred when the test exercises post-init behavior):**
>
> - Use the `postgres_state_store_url` fixture
> - Pass `env={"DATABASE_URL": postgres_state_store_url}` to `CliRunner.invoke(...)` so the spawned init resolves to the test's Postgres database
>
> [...]
>
> A helper in `tests/conftest.py` is worth adding to avoid boilerplate:
>
> ```python
> @pytest.fixture
> def cli_env(postgres_state_store_url: str) -> dict[str, str]:
>     """Env dict for CliRunner.invoke; routes the spawned process at the per-test Postgres."""
>     return {"DATABASE_URL": postgres_state_store_url}
> ```

**Proposed replacement:**

> **Option C1 (preferred when the test exercises post-init behavior):**
>
> - Use the `postgres_state_store_url` fixture
> - Pass `env={"DATABASE_URL": postgres_state_store_url}` to `CliRunner.invoke(...)`
> - **And** consume the `cli_env` fixture, which (in addition to returning the env dict) installs a function-scoped monkeypatch on `database_mod.resolve_state_store_url` so the resolved URL falls back to the per-test database URL when the bootstrap Config has no override. This shim exists because production `resolve_state_store_url` does not currently honor `DATABASE_URL` natively during `carve init` (it only honors `${DATABASE_URL}` interpolation inside an already-loaded `runtime.toml`). See the followup spec referenced below.
>
> [...]
>
> A helper in `tests/conftest.py` is required to make this work, not merely "worth adding":
>
> ```python
> @pytest.fixture
> def cli_env(
>     postgres_state_store_url: str, monkeypatch: pytest.MonkeyPatch
> ) -> dict[str, str]:
>     """Env dict for CliRunner.invoke; also patches the one runtime
>     call site for resolve_state_store_url (carve.core.state.database)
>     so the resolved URL falls back to the per-test DB when the
>     bootstrap Config has no override. The fallback closes over
>     postgres_state_store_url rather than reading os.environ at call
>     time (works around a production gap tracked in a separate
>     followup spec).
>     """
>     ...
> ```

**Justification:** The engineer's implementation discovered that the C1 env-override is not sufficient on its own — `carve init`'s bootstrap Config falls through to `DEFAULT_STATE_STORE_URL` because there's no `runtime.toml` yet to provide `${DATABASE_URL}` interpolation. The reviewer (`python-review-v0.1-01-followup.md` Suggestion 3) recommends that the right next step is a small v0.1-02-era spec that adds native `DATABASE_URL` precedence into `resolve_state_store_url`: `state_store.url override > DATABASE_URL env > legacy server.state_store > default`. Once that lands, the `_resolve_with_test_fallback` shim collapses and `cli_env` becomes a one-line dict (matching the spec's original wording). The spec should either be updated to describe what was actually built, or — if a new followup spec is going to be filed — point at it.

Post-cleanup the shim is leaner than originally described in this proposal:

- It patches a single module (`carve.core.state.database`) rather than both `state_store_mod` and `database_mod` — the dead second patch was removed since nothing in the runtime path re-imports `resolve_state_store_url` through `state_store_mod`.
- The inner function closes over the per-test URL directly instead of reading `os.environ["DATABASE_URL"]` at call time, which makes the fixture robust to a developer's shell `DATABASE_URL` leaking into post-invoke direct calls.

### Open questions

**Current spec text:**

> - **Should we add `--skip-postgres-bootstrap` to `carve init`?** *Implementation default.* No in this spec; let tests use C1 (env-override). Add the flag in a future spec if the post-init-no-Postgres workflow becomes important for offline-CI scenarios. If a test really can't use C1, the engineer can flag it.

**Proposed replacement:**

> - **Should we add `--skip-postgres-bootstrap` to `carve init`?** *Implementation default.* No in this spec; let tests use C1 (env-override). Add the flag in a future spec if the post-init-no-Postgres workflow becomes important for offline-CI scenarios. If a test really can't use C1, the engineer can flag it.
> - **Does C1's env-override actually work today against unmodified production?** *Resolved during implementation.* No — it requires the `cli_env` fixture to additionally monkeypatch `resolve_state_store_url` so the resolved URL honors `DATABASE_URL` when the bootstrap Config has no override. The production gap (env-var precedence in `resolve_state_store_url`) is tracked as a new v0.1-02-era followup spec: see `specs/v0.1/_followup-database-url-env-precedence.md` (to be filed). When that lands, the `cli_env` fixture collapses to the one-line dict version originally shown in §Bucket C.

**Justification:** Same as above. The open-questions section pre-resolved "no production change" without anticipating that the test sweep would need the production change to work. Recording the resolution prevents the same gap being re-discovered by the next contributor reading the spec.

### Acceptance

**Current spec text:**

> - No production code changes outside the docstring updates (this is a test sweep)

**Proposed replacement:**

> - No production code changes outside the docstring updates (this is a test sweep)
> - **Caveat 1 (test-side shim):** the spec landed without modifying `src/carve/core/config/state_store.py`'s behavior, but the test sweep depends on a function-scoped monkeypatch in `cli_env` that masks a production gap. The production gap is tracked in a separate followup spec (`_followup-database-url-env-precedence`); once that lands, the monkeypatch in `cli_env` becomes a no-op and can be removed.
> - **Caveat 2 (user-authorized scope expansion):** during reviewer-suggestion cleanup, `src/carve/cli/commands/init.py::_initialize_state_store` had its post-init `console.print` line updated (`{project_root}/.carve/state.db` → `state store schema initialized (postgres)`) alongside its docstring, because the original line was a SQLite-retirement straggler that contradicted what the function actually does. Small in absolute size (one print line + one docstring), but a real expansion of the spec's "test + doc work only" guardrail. The user explicitly authorized this as part of the "address all 5 reviewer suggestions" instruction. The corresponding inline callouts already live in the spec's §Files-this-spec-produces and §Acceptance sections.

**Justification:** The acceptance bar of "no production code changes" technically held in spirit, but the *literal* constraint was relaxed twice in different ways — once silently (the test-side shim that papers over a production gap) and once explicitly (the user-authorized print-line cleanup in `init.py`). Recording both deviations lets the next contributor reading the spec see exactly what shipped and what's still owed elsewhere.

## Recommendation

- [ ] Accept all proposed changes — and additionally file the followup spec for native `DATABASE_URL` precedence in `resolve_state_store_url` so the spec text's forward-pointer is concrete.
- [ ] Accept some, reject others (note which)
- [ ] Reject all — implementation should be rolled back to match the original spec.

The clearest path is **Accept all + file the followup**. The implementation is already shipped, well-tested, and reviewed PASS by all three reviewers; rolling back would mean reverting a working test sweep over a documentation issue. The followup spec to file is small (~50-100 lines of production code: change precedence order in `resolve_state_store_url`, add three unit tests in `test_state_store.py`, delete the `_resolve_with_env_fallback` shim and double-monkeypatch in `tests/conftest.py`).

This file should be reviewed by a maintainer and either applied (by hand-editing the spec to match these proposed sections, then deleting this proposal) or rejected (delete this proposal and either fix the implementation or escalate).

## Related reviewer findings (informational — not for spec text)

Five reviewer suggestions from `python-review-v0.1-01-followup.md` were adjacent to the production gap above. **All five have now landed** as part of a user-authorized "address all 5 suggestions before commit" pass on 2026-05-20. Recorded here for the proposal-reviewing maintainer; none of these are spec drift on their own.

- **JSONB stragglers fixed (5 sites).** `task_graph_json="{}"` → `task_graph_json={}` and `manifest_json='{"files": []}'` → `manifest_json={"files": []}` across `tests/cli/commands/el/test_list.py`, `tests/cli/orchestrator/test_planner.py`, `tests/cli/orchestrator/test_runner.py`, `tests/core/state/test_repository.py`, `tests/core/deploy/test_verifier.py`. Tests now pass dicts into JSONB columns rather than JSON strings; round-trip behavior matches what the runtime writes.
- **Dead double-patch removed.** `tests/conftest.py::cli_env` no longer patches `state_store_mod.resolve_state_store_url` — only `database_mod.resolve_state_store_url`. The shim shape in this proposal's §Bucket C has been updated to reflect this.
- **`cli_env` over-apply trimmed.** 10 tests in `tests/test_cli.py` (`--help`, `version`, run-removed, two stub callbacks, deploy-alias, no-carve-toml, three dotenv callbacks) no longer take the `cli_env` fixture. These tests never reach `initialize_database`, so the smoke path no longer requires Docker.
- **Production-code drift in `init.py`.** Promoted out of the "informational" pile and into this proposal's main strategic-drift discussion: the `_initialize_state_store` print line + docstring update was a user-authorized scope expansion beyond the spec's "test + doc work only" guardrail. See the §Acceptance §Caveat 2 above and the inline callouts already applied to the spec.
- **`_resolve_with_test_fallback` closes over the URL.** The fallback in `cli_env` no longer reads `os.environ["DATABASE_URL"]` at call time; it closes over `postgres_state_store_url` directly. Equivalent inside `CliRunner.invoke`, safer outside it.

The first three collapse on their own once the `_followup-database-url-env-precedence` spec lands and `cli_env` reduces to a one-line dict.
