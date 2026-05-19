# v0.1-01 — State store migration: SQLite → Postgres

> Aligns the M1 state store (SQLAlchemy + SQLite) to the v0.1 positioning's Postgres-from-day-one decision ([positioning #11](../_strategy/2026-05-positioning.md), [ARCHITECTURE §5.7](../ARCHITECTURE.md), [PROJECT_PLAN spec set item 1](../PROJECT_PLAN.md)).

## Status

- **Status:** Drafting
- **Depends on:** None (foundation spec)
- **Blocks:** every subsequent v0.1 spec — the state store is foundational
- **Audit reference:** M1-03 was HISTORICAL with a code-revision flag; this spec ships that revision

## Goal

Replace SQLite with Postgres as Carve's state store backend, in a single coherent change that:

1. Updates the SQLAlchemy engine configuration to default to Postgres
2. Audits and (where needed) revises the six existing Alembic migrations to work cleanly against Postgres
3. Ships a one-shot `carve migrate-state` CLI tool so the small set of existing walking-skeleton users with SQLite state stores can migrate without data loss
4. Updates test infrastructure to run against Postgres

After this spec lands, every subsequent v0.1 spec assumes a Postgres state store. The SQLite path remains available only as a source for the migration tool — new installs use Postgres exclusively.

## Out of scope

- The `docker-compose.yml` bundling Postgres for first-run UX — that's [v0.1-02 OSS packaging](./02-oss-packaging.md).
- New tables introduced by other v0.1 specs (jobs, asks, lineage, webhooks, archive tables, etc.) — those land in their respective specs via additional Alembic migrations on top of the post-this-spec baseline.
- Multi-tenancy `tenant_id` columns on every table — covered in [v0.1-07 runtime](./07-runtime.md) and [v0.1-09 rest-api](./09-rest-api.md) where the actual tenant-aware code paths land. This spec keeps the M1-shape schema; multi-tenancy is additive later.
- Read replicas, partitioned tables, PgBouncer configuration — those are hosted-product concerns.

## Files this spec produces

```
src/carve/core/state/database.py             # MODIFY — engine factory targets Postgres
src/carve/core/state/models.py               # MODIFY — JSONB where TEXT/JSON used; type adjustments
src/carve/cli/migrate_state.py               # NEW — `carve migrate-state` command
migrations/env.py                            # MODIFY — Postgres-aware Alembic env
migrations/versions/0001_baseline.py         # AUDIT — port to Postgres if SQLite-specific
migrations/versions/0002_pipeline_centric.py # AUDIT
migrations/versions/0003_rename_apply_to_deploy.py # AUDIT
migrations/versions/0004_build_entity.py     # AUDIT
migrations/versions/0005_runs_target.py      # AUDIT
migrations/versions/0006_recovery_chains.py  # AUDIT
alembic.ini                                  # MODIFY — connection string template is Postgres
tests/conftest.py                            # MODIFY — Postgres fixtures (testcontainers-python or sibling pattern)
tests/integration/test_migrate_state.py      # NEW — end-to-end SQLite→Postgres migration test
src/carve/core/config/state_store.py         # NEW (or extend existing config) — read state_store_url from runtime.toml + env
docs/upgrade-from-walking-skeleton.md        # NEW — user-facing migration guide
```

## Behavior

### Engine and connection

- `runtime.toml` gains a `[state_store]` section with `url = "${DATABASE_URL}"` (env-var interpolation, per [PRD §7.3](../PRD.md))
- `DATABASE_URL` defaults to `postgresql+psycopg://carve:carve@localhost:5432/carve` (matching the docker-compose bundle in [v0.1-02](./02-oss-packaging.md))
- The engine factory accepts either a fully-qualified Postgres URL or `sqlite:///<path>` as a fallback for the migration-source case **only** — runtime operation against SQLite is rejected with a clear error pointing to `carve migrate-state`
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

### The `carve migrate-state` tool

A one-shot CLI command:

```bash
carve migrate-state --from sqlite:///path/to/.carve/state.db --to postgresql+psycopg://...
```

Behavior:

1. **Validate** — connect to both sides, confirm SQLite source has expected M1-shape schema (revision matches one of 0001..0006), confirm Postgres target is empty or at the same revision after upgrade
2. **Upgrade Postgres** — run `alembic upgrade head` against the Postgres target to ensure schema matches the latest migration
3. **Copy** — for each table in dependency-safe order (Pipelines → Plans → Builds → Runs → Logs), `SELECT *` from SQLite, `INSERT INTO` Postgres in batches of 1000 rows
4. **Verify** — `SELECT COUNT(*)` on both sides per table; fail if any mismatch
5. **Report** — print summary: tables migrated, row counts, elapsed time, target URL
6. **Idempotency** — if Postgres target already has rows in a table, the tool refuses to overwrite unless `--force` is passed
7. **Non-destructive** — never deletes from SQLite. The original `.db` file is preserved as a user-managed backup. The tool prints a message recommending the user back it up to durable storage before discarding.

Edge cases:

- **Partial M1 state** — some users may have run only a subset of M1 specs and have a partial DB. The tool's validation step detects this via the Alembic version table and prints a clear error.
- **In-flight runs in SQLite** — the tool refuses to run if any `runs` row has `status IN ('running', 'queued')` on the source side, with a message instructing the user to wait for runs to complete first.
- **Postgres target unreachable** — clear error with the resolved connection string (with credentials masked) and the underlying psycopg error.

### Test infrastructure

- `tests/conftest.py` provides a `postgres_state_store` session-scoped fixture that brings up a Postgres container (via `testcontainers-python` — already a transitive dep through some test pkgs, otherwise add to `dev-dependencies`)
- Existing M1 tests that used a SQLite in-memory DB are migrated to use the Postgres fixture; assertions on JSON column shapes update to use Postgres `->`/`->>` operators where applicable
- New `tests/integration/test_migrate_state.py` creates a SQLite DB populated with synthetic M1-shape data, runs the migrator, and asserts row counts + content equivalence

### Documentation

`docs/upgrade-from-walking-skeleton.md` — a short user-facing guide covering:

- Why we moved to Postgres (one paragraph, links to positioning #11)
- Prerequisites (Docker for the bundled compose, OR a running Postgres instance)
- The migration command, with example output
- What to verify after migration (`carve runs list` should show the same recent runs)
- How to roll back (the original `state.db` is untouched; revert by changing `DATABASE_URL` back to SQLite — but the runtime will refuse to start, so the only realistic rollback is `git checkout` to a pre-v0.1.0 commit)

## Tests

- **Unit:** model definitions import and metadata reflection works against Postgres
- **Unit:** engine factory rejects non-Postgres URLs at runtime (`sqlite:///` → friendly error pointing at `carve migrate-state`)
- **Unit:** migration tool's validation step correctly identifies a partial-M1 source
- **Integration:** start with an empty Postgres, `alembic upgrade head` → schema matches expected DDL
- **Integration:** start with a populated SQLite (synthetic M1-shape data, ~100 rows across all tables), run migrator → row counts match on both sides
- **Integration:** rerun migrator against already-populated Postgres → refuses with non-zero exit (and proceeds with `--force`)
- **Integration:** existing M1 test suite passes against Postgres (this is the regression bar)

## Acceptance

- A fresh `carve serve` against an empty Postgres bootstraps the schema and accepts plans/builds/runs
- An existing walking-skeleton user with `.carve/state.db` can run `carve migrate-state --from sqlite:///.carve/state.db --to ${DATABASE_URL}` and have all their history available in Postgres afterward
- The full M1 test suite passes against Postgres
- `carve serve` with a SQLite `DATABASE_URL` fails immediately with a friendly error mentioning `carve migrate-state`
- Zero data loss in the migration tool's verify step across all six tables
- `docs/upgrade-from-walking-skeleton.md` walks a user through the migration in under 15 minutes

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
