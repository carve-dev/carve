# First run with bundled Postgres

The fastest way to get Carve running if you have Docker but don't already
operate a Postgres instance. `carve init` drops a Postgres-only
`docker-compose.yml`; Carve itself runs natively via `carve serve` (it is a
Python CLI, not a container).

> **Dev-only credentials.** The bundled compose uses `carve` / `carve` and
> binds Postgres to `127.0.0.1` only. It is for local dev / single-team
> self-host — never expose it to the network. For anything production-shaped,
> use [external Postgres](./first-run-with-external-postgres.md).

## Prerequisites

- [Carve installed](./installation.md) (`pipx install carve` / `uv tool install carve` / `pip install carve`)
- Docker (with `docker compose`)

## Steps

```bash
mkdir my-project && cd my-project
carve init                       # writes carve.toml, carve/, .env.example, docker-compose.yml
```

`carve init` renders the bundled `docker-compose.yml` and adds a
`DATABASE_URL` line to `.env.example` matching it. Because Postgres isn't up
yet, init prints a next-step instead of migrating — that's expected.

```bash
cp .env.example .env             # then edit .env: set ANTHROPIC_API_KEY (or run `carve auth login`)
docker compose up -d             # starts Postgres on 127.0.0.1:5432
carve serve                      # brings the schema to head, then runs the API/scheduler/worker
```

In another terminal:

```bash
carve plan "ingest the Stripe charges API"
```

## Overriding the defaults

Set these in `.env` before `docker compose up -d`:

- `POSTGRES_PASSWORD` — overrides the default `carve` password (also update `DATABASE_URL`).
- `CARVE_POSTGRES_PORT` — overrides the host port (default `5432`) if it's taken.

## Lifecycle

```bash
docker compose stop              # stop Postgres, keep the data volume
docker compose down              # remove the container, keep the named volume (data preserved)
docker compose down -v           # also remove the volume — DATA LOSS
```

Carve doesn't wrap these — they're standard Docker Compose commands. Re-running
`carve init` leaves an existing `docker-compose.yml` untouched (delete it first
if you want a fresh template).

## Production considerations

The bundled Postgres is unmanaged: no automatic backups, no tuning, no
replication. For anything beyond local dev:

- The named volume (`carve-postgres-data-<project>`) survives `docker compose down`; only `down -v` destroys it.
- For backups, run `pg_dump` on a schedule, or — better — move to a managed instance with [external Postgres](./first-run-with-external-postgres.md).
