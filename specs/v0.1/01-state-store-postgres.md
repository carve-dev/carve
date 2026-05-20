# v0.1-01 — State store: Postgres (SQLite retired)

> Aligns the M1 state store (SQLAlchemy + SQLite) to the v0.1 positioning's Postgres-from-day-one decision ([positioning #11](../_strategy/2026-05-positioning.md), [ARCHITECTURE §5.7](../ARCHITECTURE.md), [PROJECT_PLAN spec set item 1](../PROJECT_PLAN.md)).

## Status

- **Status:** Mostly landed (2026-05-19); M1 test sweep deferred to v0.1-01-followup
- **Depends on:** None (foundation spec)
- **Blocks:** every subsequent v0.1 spec — the state store is foundational
- **Audit reference:** M1-03 was HISTORICAL with a code-revision flag; this spec ships that revision

## Goal

Replace SQLite with Postgres as Carve's state store backend, in a single coherent change that:

1. Updates the SQLAlchemy engine configuration to default to Postgres
2. Audits and (where needed) revises the six existing Alembic migrations to work cleanly against Postgres
3. Updates test infrastructure to run against Postgres (via testcontainers-python)

After this spec lands, every subsequent v0.1 spec assumes a Postgres state store. SQLite is retired outright — the engine factory rejects any non-Postgres URL.

### No migration tool

The original draft of this spec included a one-shot `carve migrate-state` CLI for M1 walking-skeleton users. **That tool was removed during implementation** when it became clear that Carve has no released users yet, so there is no SQLite data anywhere that needs preserving. Local-dev `.carve/state.db` files from prior development can be deleted safely; new Postgres installs start empty.

The original migrator code is preserved in git history at commit `23bcf88` for reference. If a future release ever needs to support live data migration (e.g., for a self-hosted-to-hosted-product import path), that's a new spec.

## Out of scope

- A SQLite → Postgres migration tool. Removed — see *No migration tool* above.
- The `docker-compose.yml` bundling Postgres for first-run UX — that's [v0.1-02 OSS packaging](./02-oss-packaging.md).
- New tables introduced by other v0.1 specs (jobs, asks, lineage, webhooks, archive tables, etc.) — those land in their respective specs via additional Alembic migrations on top of the post-this-spec baseline.
- Multi-tenancy `tenant_id` columns on every table — covered in [v0.1-07 runtime](./07-runtime.md) and [v0.1-09 rest-api](./09-rest-api.md) where the actual tenant-aware code paths land. This spec keeps the M1-shape schema; multi-tenancy is additive later.
- Read replicas, partitioned tables, PgBouncer configuration — those are hosted-product concerns.
- Migrating M1-era test fixtures from SQLite-backed `_make_config` helpers to the new Postgres fixture. Spec implied this work but the scope ballooned past the iteration budget; deferred to **v0.1-01-followup** (see *Deferred work* at the end of this spec).

## Files this spec produces

```
src/carve/core/state/database.py             # MODIFY — engine factory targets Postgres; rejects everything else
src/carve/core/state/models.py               # MODIFY — JSONB where TEXT/JSON used; TIMESTAMPTZ for timestamps
src/carve/core/state/repository.py           # MODIFY — JSONB write fix (manifest_json no longer json.dumps'd)
migrations/env.py                            # MODIFY — Postgres-aware Alembic env
migrations/versions/0001_baseline.py         # AUDIT — port to Postgres if SQLite-specific
migrations/versions/0002_pipeline_centric.py # AUDIT
migrations/versions/0003_rename_apply_to_deploy.py # AUDIT
migrations/versions/0004_build_entity.py     # AUDIT
migrations/versions/0005_runs_target.py      # AUDIT
migrations/versions/0006_recovery_chains.py  # AUDIT
alembic.ini                                  # MODIFY — connection string template is Postgres
tests/conftest.py                            # MODIFY — Postgres fixtures (testcontainers-python)
src/carve/core/config/state_store.py         # NEW — read state_store_url from runtime.toml + env
docs/installation.md                         # NEW — first-install walkthrough (bundled + external Postgres)
```

Plus the JSONB call-site sweep (6 readers + 2 writers) and the `.replace(tzinfo=None)` removal across 12 sites — these are derived consequences of the JSONB and TIMESTAMPTZ shifts and didn't get their own per-file entries in the original draft. Files touched by that sweep:

- `src/carve/cli/commands/build.py`, `el/deploy.py`, `el/verify.py`, `el/list.py`
- `src/carve/cli/orchestrator/builder.py`, `planner.py`
- `src/carve/core/deploy/preflight.py`

## Behavior

### Engine and connection

- `runtime.toml` gains a `[state_store]` section with `url = "${DATABASE_URL}"` (env-var interpolation, per [PRD §7.3](../PRD.md))
- `DATABASE_URL` defaults to `postgresql+psycopg://carve:carve@localhost:5432/carve` (matching the docker-compose bundle in [v0.1-02](./02-oss-packaging.md))
- The engine factory accepts **Postgres URLs only**. Any other scheme (sqlite, mysql, anything) raises `StateStoreBackendError` with a friendly message pointing at `docs/installation.md`.
- Engine creation uses a connection pool sized for the expected worker count (default pool size 10, max overflow 20; configurable in `[state_store]` block)

### Model changes

The existing six tables (Run, Log, Plan, Pipeline, Build, plus the recovery-chain additions) are preserved in shape. Type changes only:

| Field                     | Was              | Becomes             | Why                                                  |
|---------------------------|------------------|---------------------|------------------------------------------------------|
| `Plan.task_graph`         | TEXT (JSON)      | JSONB               | Native JSON ops on Postgres; smaller storage         |
| `Plan.file_diffs`         | TEXT (JSON)      | JSONB               | Same                                                 |
| `Build.manifest_json`     | TEXT (JSON)      | JSONB               | Same                                                 |
| `Run.error_message`       | TEXT             | TEXT                | unchanged                                            |
| Timestamps                | naive UTC        | `TIMESTAMP WITH TIME ZONE` | Postgres native tz support; ARCHITECTURE §4.2 expects it for `scheduled_for`-style fields |

Naive UTC was a SQLite portability concession; Postgres handles `TIMESTAMPTZ` natively and v0.1 step types (especially `dlt`'s schedule semantics) benefit from real tz handling. All ORM-side accessors continue to return UTC.

### Alembic migration audit

Walk each of `0001` through `0006` and verify:

- No SQLite-specific column types (e.g., `INTEGER` autoincrement quirks; use SQLAlchemy `Integer` + `autoincrement=True`)
- No SQLite-specific defaults (use `server_default=sa.func.now()` not `default=datetime.utcnow`)
- No CHECK constraints that rely on SQLite's looser type semantics
- No `op.batch_alter_table` blocks unless required (SQLite needs them; Postgres prefers direct ALTER)

Where a migration is clean, leave it alone. Where it isn't, rewrite the relevant `op.*` calls. Bumping the migration's `revision` is not necessary — the chain stays continuous.

### Test infrastructure

- `tests/conftest.py` provides a session-scoped Postgres container fixture (`_postgres_container`) and a per-test database fixture (`postgres_state_store_url`) via `testcontainers-python`. The session container amortizes the 5–10s container startup cost; per-test `CREATE DATABASE` is sub-100ms.
- A `postgres_config` fixture builds a minimal `Config` pointing at the per-test database for tests that previously built `ServerConfig(state_store=...)` manually.
- Migrating the rest of the M1-era tests from their SQLite-based `_make_config` helpers to these fixtures is deferred to v0.1-01-followup (see *Deferred work*).

### Documentation

`docs/installation.md` — first-install walkthrough covering:

- Python install via `pipx` / `uv tool` / `pip`
- Bundled Postgres path: `carve init` → `docker compose up -d` → `carve serve`
- External Postgres path: `carve init --external-postgres <url>` → `carve serve`
- Verification: `/healthz`, `/readyz`, `carve runs list`
- Note that a stale `.carve/state.db` from earlier development can be deleted safely

## Tests

- **Unit:** model definitions import and metadata reflection works against Postgres
- **Unit:** engine factory rejects non-Postgres URLs at runtime with a friendly message pointing at `docs/installation.md`
- **Integration:** start with an empty Postgres, `alembic upgrade head` → schema matches expected DDL
- **Integration:** existing M1 test suite passes against Postgres (this is the regression bar — see *Deferred work*)

## Acceptance

- A fresh `carve serve` against an empty Postgres bootstraps the schema and accepts plans/builds/runs
- The full M1 test suite passes against Postgres (gated by v0.1-01-followup)
- `carve serve` with any non-Postgres `DATABASE_URL` fails immediately with a friendly error pointing at the docker-compose path or external-Postgres flag
- `docs/installation.md` walks a new user from `pip install carve` to a green `/healthz` in under 15 minutes

## Deferred work

The following items were spec'd but are deferred to **v0.1-01-followup** (or folded into v0.1-02 OSS packaging where overlap makes sense):

1. **M1 test fixture sweep.** ~7 test files build a local `_make_config` helper that constructs `ServerConfig(state_store="sqlite:///...")`. The new engine factory rejects SQLite, so these tests fail at fixture-creation time. Each needs to thread the `postgres_state_store_url` fixture through and pass it into the Config. Fix plan at [`.carve-build/fixes/v0.1-01-iter1.md`](../../.carve-build/fixes/v0.1-01-iter1.md) enumerates the files (Buckets A/B/C). Partial sweep landed in commit `23bcf88` (3 files: `test_pipelines.py`, `test_listing.py`, `test_recovery.py` plus `test_extract_load_agent._config()` signature change). Remaining: ~4 mechanical files + 3 Bucket B rewrites + 1 Bucket C semantic rewrite.
2. **Three new unit tests** for spec ## Tests bullets that have no dedicated coverage: model metadata reflection (bullet 1), engine factory rejection of non-Postgres URLs (bullet 2; `grep StateStoreBackendError tests/` currently returns zero hits), `alembic upgrade head` schema-shape assertion on empty Postgres (bullet 3).
3. **Security-reviewer Informational** findings: superuser-requirement note (no longer relevant — the migrator used `SET LOCAL session_replication_role = 'replica'` which required superuser; that code is gone). The dev-only label on `DEFAULT_STATE_STORE_URL` carries forward as a small follow-up.

## Design notes

- **Why Postgres-only at runtime instead of supporting both?** Because the v0.1 runtime (spec 07) depends on Postgres-specific features (partial unique indexes, `FOR UPDATE SKIP LOCKED`, JSONB) that have no clean SQLite equivalents. Supporting both would force the runtime to maintain two code paths, which defeats the simplicity dividend of Postgres-from-day-one.
- **Why naive UTC → tz-aware?** SQLite drops `tzinfo` on round-trip, so M1 stored naive UTC and reattached `UTC` at the boundary. Postgres handles `TIMESTAMPTZ` natively; v0.1 step types (especially scheduled runs) benefit from real tz handling. The ORM-side return shape stays the same (UTC datetimes), so application code is unchanged.
- **Why a separate migration tool instead of in-place automatic migration?** The M1 user base is small enough that we can ask them to run one command, and the explicit gating prevents surprise behavior on `carve serve` startup (e.g., long migration delays, partial state on failure). A one-shot tool is easier to test, easier to reason about, and easier to recover from than auto-migration.
- **Why not bump Alembic revision IDs?** The migration chain (0001..0006) remains the canonical history. Audits that rewrite operator calls inside an existing migration keep the same revision so users don't see a phantom "migration ran" entry. New v0.1 tables get fresh revisions (0007 onward) in their respective specs.

## Open questions

> Tagged **Strategy-required** (needs explicit user decision before the spec ships) or **Implementation default** (Claude Code picks a reasonable default during `/build-spec`; user confirms or redirects in PR review).

- **`testcontainers-python` dependency.** *Implementation default.* Use it; the standard pattern; ~5–10s overhead on cold runs, negligible after first use via reuse. Swap to `pytest-postgresql` or `docker-py` if CI feels slow in practice.
- **Connection pool sizing defaults.** *Implementation default.* Start with 10/20 (pool/overflow). With the v0.1 default of 1 worker we won't even stress 5 connections. Revisit when [v0.1-07 runtime](./07-runtime.md) lands with concrete worker counts.
- **Auto-migrate on `carve serve` startup vs explicit flag.** *Implementation default.* OSS: auto-migrate on startup (M1's current behavior; matches the dbt-core model for friendliness). Hosted: explicit out-of-band step. The OSS path is what this spec ships; hosted overrides via its own startup flow.
