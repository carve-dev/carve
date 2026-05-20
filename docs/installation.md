# Installing Carve

Carve is a Python CLI plus a Postgres-backed state store. v0.1 is
Postgres-from-day-one (no SQLite fallback — see [spec
v0.1-01](../specs/v0.1/01-state-store-postgres.md) for rationale).

## Requirements

- Python 3.11+
- Postgres 14+ — either the bundled `docker-compose.yml` (recommended for
  local dev) or your own (managed RDS, Cloud SQL, Supabase, etc.)
- Docker — only if you use the bundled Postgres path

## Install

Pick your favorite Python tool:

```bash
$ pipx install carve
# or
$ uv tool install carve
# or
$ pip install --user carve
```

After install, `carve --version` should work.

## First-run with bundled Postgres

```bash
$ mkdir my-carve-project && cd my-carve-project
$ carve init
$ docker compose up -d           # starts Postgres on 127.0.0.1:5432
$ carve serve                    # API + scheduler + worker
```

In another terminal:

```bash
$ carve plan "ingest the Stripe charges API"
```

Behind the scenes:

- `carve init` writes `carve.toml`, `carve/`, `.env.example`, and
  `docker-compose.yml` (the bundled-Postgres template — see [spec
  v0.1-02](../specs/v0.1/02-oss-packaging.md)).
- `docker compose up -d` brings up Postgres at the URL the `.env`
  template expects: `postgresql+psycopg://carve:carve@127.0.0.1:5432/carve`.
- `carve serve` runs `alembic upgrade head` on the empty Postgres,
  then starts accepting CLI / REST / MCP requests.

## First-run with external Postgres

If you already operate Postgres (managed RDS / Cloud SQL / Supabase / a
local install you don't want a docker-compose duplicate of):

```bash
$ carve init --external-postgres "postgresql+psycopg://user:pass@host:5432/db"
$ carve serve
```

The `--external-postgres` flag tells `carve init` to skip the
`docker-compose.yml` scaffolding and record your connection string in
`.env` instead.

## What if I have an old `.carve/state.db` from earlier development?

It's safe to ignore. v0.1 doesn't read it, and Carve has no released
users yet so there's no migration story to preserve. If you want to
clear it:

```bash
$ rm -rf .carve/state.db
```

Your fresh Postgres install starts empty.

## Verifying the install

```bash
$ curl -s http://127.0.0.1:8765/healthz   # liveness
$ curl -s http://127.0.0.1:8765/readyz    # readiness (200 once migrations
                                          # are at head)
$ carve runs list
```

If `readyz` returns 503, check `carve serve` logs — the most common
cause is `DATABASE_URL` pointing somewhere unreachable.

## Where to go next

- [`docs/runtime.md`](./runtime.md) — how the scheduler + workers work
- [`docs/api-reference.md`](./api-reference.md) — REST surface (or visit
  `/api/docs` for live Swagger)
- [`docs/mcp-server.md`](./mcp-server.md) — drive Carve from Claude
  Desktop / Cursor / Claude Code
