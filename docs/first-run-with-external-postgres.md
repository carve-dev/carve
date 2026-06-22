# First run with external Postgres

If you already operate Postgres — managed RDS, Cloud SQL, Supabase, or a local
install — point Carve at it with `--external-postgres` and skip Docker
entirely.

## Prerequisites

- [Carve installed](./installation.md)
- A reachable Postgres and a connection string for it
- The connecting user can **CREATE TABLE** (Carve runs Alembic migrations)

## Steps

```bash
mkdir my-project && cd my-project
carve init --external-postgres "postgresql+psycopg://user:pass@host:5432/db"
```

`carve init --external-postgres`:

- validates the connection string (must start with `postgresql+psycopg://` or `postgresql://` — the latter is upgraded to `+psycopg` for you);
- **does not** write a `docker-compose.yml`;
- connects and **migrates the schema to head** immediately (this also confirms the user has CREATE TABLE);
- writes a **commented placeholder** to `.env.example` and **prints** the real `DATABASE_URL` line for you to paste into your gitignored `.env` — the real, password-bearing URL is never written to a committed file.

If the database is unreachable or the user can't create tables, init fails
(exit 3) with the reason — fix the URL/privileges and re-run.

```bash
cp .env.example .env             # then edit .env: set ANTHROPIC_API_KEY (or run `carve auth login`)
carve serve
```

In another terminal:

```bash
carve plan "ingest the Stripe charges API"
```

## Notes

- **Carve does not manage your Postgres lifecycle** — backups, upgrades,
  tuning, and access control are your responsibility.
- Keep the password-bearing `DATABASE_URL` in `.env` (gitignored), not in
  `.env.example` or any committed file.
- To switch which database Carve uses later, change `DATABASE_URL` in `.env`
  (it takes precedence over the bundled default).
