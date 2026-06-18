# State store: Postgres (SQLite retired)

> Aligns the M1 state store (SQLAlchemy + SQLite) to the positioning's Postgres-from-day-one decision ([positioning #11](../_strategy/2026-05-positioning.md), [ARCHITECTURE §5.7](../ARCHITECTURE.md)).

## Status

> **Updated during implementation (2026-06-18):** the M1 test-fixture sweep referenced here is complete (verified against the test suite — every state-store-touching test threads the `postgres_state_store_url` fixture; no test builds a live SQLite store). Status updated from "mostly landed" to "landed". The one remaining follow-up is the cosmetic dev-only label on `DEFAULT_STATE_STORE_URL` (see *Deferred work* #3).

- **Status:** Landed (2026-05-19; test-fixture sweep + new unit tests confirmed 2026-06-18)
- **Depends on:** None (foundation spec)
- **Blocks:** every subsequent capability spec — the state store is foundational
- **Audit reference:** M1-03 was HISTORICAL with a code-revision flag; this spec ships that revision

## Goal

Replace SQLite with Postgres as Carve's state store backend, in a single coherent change that:

1. Updates the SQLAlchemy engine configuration to default to Postgres
2. Audits and (where needed) revises the six existing Alembic migrations to work cleanly against Postgres
3. Updates test infrastructure to run against Postgres (via testcontainers-python)

After this spec lands, every subsequent capability spec assumes a Postgres state store. SQLite is retired outright — the engine factory rejects any non-Postgres URL.

### No migration tool

The original draft of this spec included a one-shot `carve migrate-state` CLI for M1 walking-skeleton users. **That tool was removed during implementation** when it became clear that Carve has no released users yet, so there is no SQLite data anywhere that needs preserving. Local-dev `.carve/state.db` files from prior development can be deleted safely; new Postgres installs start empty.

The original migrator code is preserved in git history at commit `23bcf88` for reference. If a future release ever needs to support live data migration (e.g., for a self-hosted-to-hosted-product import path), that's a new spec.

## Out of scope

- A SQLite → Postgres migration tool. Removed — see *No migration tool* above.
- The `docker-compose.yml` bundling Postgres for first-run UX — that's [packaging OSS packaging](./packaging.md).
- New tables introduced by other capability specs (jobs, asks, lineage, webhooks, archive tables, etc.) — those land in their respective specs via additional Alembic migrations on top of the post-this-spec baseline.
- Multi-tenancy `tenant_id` columns on every table — covered in [runtime](./runtime.md) and [rest-api](./rest-api.md) where the actual tenant-aware code paths land. This spec keeps the M1-shape schema; multi-tenancy is additive later.
- Read replicas, partitioned tables, PgBouncer configuration — those are hosted-product concerns.
- Migrating M1-era test fixtures from SQLite-backed `_make_config` helpers to the new Postgres fixture. Spec implied this work but the scope ballooned past the iteration budget; deferred to **state-store** (see *Deferred work* at the end of this spec).

## Behavior

### Engine and connection

- `runtime.toml` gains a `[state_store]` section with `url = "${DATABASE_URL}"` (env-var interpolation, per [PRD §7.3](../PRD.md))
- `DATABASE_URL` defaults to `postgresql+psycopg://carve:carve@localhost:5432/carve` (matching the docker-compose bundle in [packaging](./packaging.md))
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

Naive UTC was a SQLite portability concession; Postgres handles `TIMESTAMPTZ` natively and Carve's step types (especially `dlt`'s schedule semantics) benefit from real tz handling. All ORM-side accessors continue to return UTC.

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
- Migrating the rest of the M1-era tests from their SQLite-based `_make_config` helpers to these fixtures is deferred to state-store (see *Deferred work*).

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
- The full M1 test suite passes against Postgres (gated by state-store)
- `carve serve` with any non-Postgres `DATABASE_URL` fails immediately with a friendly error pointing at the docker-compose path or external-Postgres flag
- `docs/installation.md` walks a new user from `pip install carve` to a green `/healthz` in under 15 minutes

## Deferred work

> **Updated during implementation (2026-06-18):** items #1 and #2 below are **done** — verified against the code and a green test run, not just claimed. The fixture sweep landed in full (no remaining SQLite-backed fixtures; the whole suite threads `postgres_state_store_url`) and all three unit/integration tests now exist. They are marked DONE inline below rather than deleted, so the history of what was deferred stays legible. Item #3 (the dev-only label on `DEFAULT_STATE_STORE_URL`) is the lone remaining open follow-up; it is cosmetic and non-blocking.

The following items were spec'd as deferred. #1 and #2 have since landed; #3 remains open:

1. **M1 test fixture sweep. — DONE (2026-06-18).** The sweep is complete: no test builds a live SQLite `ServerConfig(state_store="sqlite:///...")`. Every state-store-touching helper (`_make_config` / `_config` / `_build_config` across el / recovery / targets / snowflake / config / orchestrator tests) threads the `postgres_state_store_url` fixture (or `postgres_config`) through into the Config. The `ServerConfig(state_store=...)` field name persists as the legacy alias, but the resolved URL is always Postgres. The original partial sweep landed in commit `23bcf88`; the remainder followed. (The earlier "~4 mechanical + 3 Bucket B + 1 Bucket C remaining" estimate was a pre-completion snapshot and no longer applies.)
2. **Three new unit tests — DONE (2026-06-18).** All three now exist and pass: model metadata reflection (`test_model_metadata_reflects_against_postgres`), engine-factory rejection of non-Postgres URLs (`test_engine_factory_rejects_non_postgres_url`), and the `alembic upgrade head` schema-shape assertion on empty Postgres (`test_alembic_upgrade_head_on_empty_postgres`). The old "`grep StateStoreBackendError tests/` returns zero hits" note is stale — `StateStoreBackendError` is now covered in `tests/core/state/test_database.py`.
3. **Dev-only label on `DEFAULT_STATE_STORE_URL` — OPEN.** A small, cosmetic, non-blocking follow-up: label `DEFAULT_STATE_STORE_URL` as dev-only so it reads clearly as a local-development default rather than a production connection string. (The related security-reviewer superuser-requirement note is no longer relevant — the migrator that used `SET LOCAL session_replication_role = 'replica'` is gone.)

## Design notes

- **Why Postgres-only at runtime instead of supporting both?** Because the runtime (spec 07) depends on Postgres-specific features (partial unique indexes, `FOR UPDATE SKIP LOCKED`, JSONB) that have no clean SQLite equivalents. Supporting both would force the runtime to maintain two code paths, which defeats the simplicity dividend of Postgres-from-day-one.
- **Why naive UTC → tz-aware?** SQLite drops `tzinfo` on round-trip, so M1 stored naive UTC and reattached `UTC` at the boundary. Postgres handles `TIMESTAMPTZ` natively; Carve's step types (especially scheduled runs) benefit from real tz handling. The ORM-side return shape stays the same (UTC datetimes), so application code is unchanged.
- **Why a separate migration tool instead of in-place automatic migration?** The M1 user base is small enough that we can ask them to run one command, and the explicit gating prevents surprise behavior on `carve serve` startup (e.g., long migration delays, partial state on failure). A one-shot tool is easier to test, easier to reason about, and easier to recover from than auto-migration.
- **Why not bump Alembic revision IDs?** The migration chain (0001..0006) remains the canonical history. Audits that rewrite operator calls inside an existing migration keep the same revision so users don't see a phantom "migration ran" entry. New tables get fresh revisions (0007 onward) in their respective specs.

## Open questions

> Tagged **Strategy-required** (needs explicit user decision before the spec ships) or **Implementation default** (Claude Code picks a reasonable default during `/build-spec`; user confirms or redirects in PR review).

- **`testcontainers-python` dependency.** *Implementation default.* Use it; the standard pattern; ~5–10s overhead on cold runs, negligible after first use via reuse. Swap to `pytest-postgresql` or `docker-py` if CI feels slow in practice.
- **Connection pool sizing defaults.** *Implementation default.* Start with 10/20 (pool/overflow). With the default of 1 worker we won't even stress 5 connections. Revisit when [runtime](./runtime.md) lands with concrete worker counts.
- **Auto-migrate on `carve serve` startup vs explicit flag.** *Implementation default.* OSS: auto-migrate on startup (M1's current behavior; matches the dbt-core model for friendliness). Hosted: explicit out-of-band step. The OSS path is what this spec ships; hosted overrides via its own startup flow.
