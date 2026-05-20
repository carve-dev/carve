# Upgrade from the M1 walking skeleton to v0.1 (SQLite → Postgres)

If you've been running Carve at any commit between M1 and v0.1-01, your state
store is SQLite (`.carve/state.db`). v0.1 requires Postgres — see [positioning
decision #11](../specs/_strategy/2026-05-positioning.md) for the rationale.
This guide walks you through the one-shot migration.

## Why we moved

Three reasons (full discussion in [spec v0.1-01](../specs/v0.1/01-state-store-postgres.md)):

1. **Multi-worker concurrency.** The new runtime ([spec v0.1-07](../specs/v0.1/07-runtime.md))
   uses `FOR UPDATE SKIP LOCKED` and partial unique indexes — both Postgres
   features without clean SQLite equivalents.
2. **OSS-to-hosted continuity.** Hosted Carve runs on Postgres. Aligning the
   OSS path makes the upgrade to managed hosting a connection-string change,
   not a data migration.
3. **Bundled docker-compose makes Postgres zero-config for first-run.** New
   installs run `docker compose up -d` and have Postgres on `127.0.0.1:5432`
   automatically (see [spec v0.1-02](../specs/v0.1/02-oss-packaging.md)).

## Prerequisites

- Carve at v0.1-01 or later (`carve --version`)
- A target Postgres instance — either the bundled docker-compose
  (`docker compose up -d`) or your own (managed RDS, Cloud SQL, Supabase, etc.)
- `DATABASE_URL` set in your `.env` or shell, e.g.:
  ```bash
  DATABASE_URL=postgresql+psycopg://carve:carve@127.0.0.1:5432/carve
  ```

## Quick check before you start

```bash
$ carve runs list --status running
```

The migrator refuses to run if any rows in `runs` have `status='running'` or
`status='queued'`. Wait for in-flight runs to complete (or `carve runs cancel`
them) before migrating.

## Run the migration

```bash
$ carve migrate-state \
    --from sqlite:///.carve/state.db \
    --to "$DATABASE_URL"
```

The tool does six things, in order:

1. **Validate** — connects to both sides; checks the SQLite revision matches
   one of `0001`–`0006`; checks Postgres is empty (or `--force` is set).
2. **Upgrade** — runs `alembic upgrade head` against Postgres so the schema
   matches v0.1.
3. **Copy** — reads from SQLite, inserts into Postgres in batches of 1000
   rows. Table order respects FK dependencies: `pipelines` → `plans` →
   `builds` → `runs` → `logs`.
4. **Verify** — runs `SELECT COUNT(*)` on both sides; refuses to declare
   success if any table's count differs.
5. **Report** — prints a per-table summary with row counts and elapsed time.
6. **Done** — exits 0. Your `.carve/state.db` is left untouched (never
   deleted by the tool).

Expected output for a small-team install (~7,000 runs, ~30,000 logs):

```
✓ Validation passed (source revision: 0006, target empty)
✓ Schema upgrade complete (head: 0006)
✓ Copied tables:
    pipelines       12 rows
    plans          124 rows
    builds          73 rows
    runs         6,847 rows
    logs        31,209 rows
✓ Row counts verified
✓ Migration complete in 47 seconds
```

## After the migration

1. Start Carve against the new Postgres:
   ```bash
   $ docker compose up -d  # if using bundled Postgres
   $ carve serve
   ```
2. Verify recent runs are visible:
   ```bash
   $ carve runs list --limit 10
   ```
3. Back up `.carve/state.db` somewhere durable. The tool didn't delete it, but
   we recommend moving it out of the project directory once you've verified the
   migration so you don't accidentally point at it later.

## Rollback

The migration tool is non-destructive — your `.carve/state.db` is preserved.
However, Carve **at v0.1-01 or later won't start against SQLite** (the engine
explicitly rejects `sqlite://` URLs at runtime, with an error pointing here).

To roll back, the only path is:

```bash
$ git checkout <pre-v0.1.0-tag>
$ # remove DATABASE_URL from .env
$ carve serve  # uses the embedded SQLite again
```

A v2 of this guide will cover live rollback once the hosted product ships and
the migration story matures.

## Common issues

### "Source revision is `0005`, not `0006`"

Your SQLite database is on an older M1.1 schema. Bring it to head first:

```bash
$ alembic -c alembic.ini upgrade head
```

(with `DATABASE_URL=sqlite:///.carve/state.db`), then re-run `carve
migrate-state`.

### "Postgres target already has rows"

Either Postgres was previously migrated (rerun is a no-op; you're done) or you
have a partial earlier attempt. Use `--force` to overwrite (destroys current
Postgres state):

```bash
$ carve migrate-state --from sqlite:///.carve/state.db --to "$DATABASE_URL" --force
```

Use `--force` only if you're sure the Postgres data isn't authoritative.

### "Refusing: 3 runs in 'running' or 'queued' state"

Wait for runs to complete or cancel them, then re-run. See `carve runs list
--status running`.

### Connection refused on the Postgres target

Check `DATABASE_URL` resolves and the Postgres instance is reachable. For the
bundled compose path:

```bash
$ docker compose ps  # carve-postgres should be running
$ pg_isready -h 127.0.0.1 -U carve  # should return "accepting connections"
```

## Where to ask

- [`docs/runtime-troubleshooting.md`](./runtime-troubleshooting.md) for
  ongoing Postgres concerns (post-migration)
- GitHub Discussions for migration-time questions
