# M3-11 — Example projects

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 2 days
**Dependencies:** all M1+M2 features; M3-01 (multi-step pipelines)

## Purpose

Three reference repositories that demonstrate Carve end-to-end across different scenarios. Examples are how new users get oriented; "clone and run" beats "read the docs" by a wide margin. Each example must run on a free Snowflake trial in under 10 minutes after `carve init`.

## The three examples

### Example 1: `carve-example-ecommerce` — greenfield

**Scenario:** small e-commerce shop with CSVs of orders, customers, and products in S3. Wants a `mart_revenue` table updated daily.

**What it shows:**
- `carve init` from scratch (no existing dbt)
- Multi-source ingest (3 CSVs → 3 staging tables)
- dbt staging → intermediate → mart pattern
- Generic tests (`unique`, `not_null`, `relationships`)
- Daily schedule with backfill capability
- Slack notification on failure

**Setup time goal:** 5 minutes from `git clone` to `carve build`.

**Repo structure:**
```
carve-example-ecommerce/
├── README.md                    Quickstart + screenshots
├── carve.toml
├── carve/
│   ├── connections.toml         Reference $SNOWFLAKE_* env vars
│   ├── pipelines/
│   │   └── daily_revenue.toml
│   └── conventions.md
├── data/                        Sample CSVs (small, in repo)
│   ├── orders.csv               (1000 rows)
│   ├── customers.csv            (200 rows)
│   └── products.csv             (50 rows)
├── models/
│   ├── staging/
│   │   ├── stg_orders.sql
│   │   ├── stg_customers.sql
│   │   ├── stg_products.sql
│   │   └── _stg_models.yml
│   ├── intermediate/
│   │   └── int_order_items_enriched.sql
│   └── marts/
│       ├── mart_revenue.sql
│       └── _mart_models.yml
├── tests/
│   └── assert_revenue_positive.sql
└── dbt_project.yml
```

The README walks through:
1. Prerequisites (Snowflake trial, Python 3.11+)
2. `git clone` + `cd`
3. Set env vars (one-liner)
4. `carve init --import .` (Carve detects the existing dbt project)
5. `carve build`
6. View the results

### Example 2: `carve-example-brownfield` — onboarding into existing dbt

**Scenario:** a real-world dbt project with ~25 models, mixed conventions, no Carve. Demonstrates the brownfield onboarding flow.

**Source:** fork of [dbt-labs/jaffle_shop](https://github.com/dbt-labs/jaffle_shop) with Carve added on top.

**What it shows:**
- `carve init` against an existing dbt repo
- Convention inference (jaffle_shop uses `stg_`, `dim_`, `fct_` prefixes)
- Generated `carve/conventions.md` matching what's there
- Adding one new pipeline alongside existing dbt models
- `carve plan "add a customer LTV mart"` produces a PR that respects existing conventions

**The narrative:** "I have dbt, I want AI agents that respect my existing setup."

The README highlights what Carve *did not* change:
- `dbt_project.yml`: untouched
- model file naming: matches existing
- macro patterns: reused
- profile: still works with `dbt run` standalone

### Example 3: `carve-example-data-platform` — the full picture

**Scenario:** small data platform with multi-source ingest (Postgres replica, S3 events, HTTP API), 4 dbt marts, freshness checks, Slack alerts, and a weekly backfill.

**What it shows:**
- All step types in one project: `python` (Postgres extract), `http` (API pull), `sql` (raw transforms), `dbt` (modeling), `shell` (custom upload)
- Multiple pipelines (daily ingest, hourly events, weekly backfill)
- Quality agent in action (test generation)
- MCP integration (one external tool — e.g. PagerDuty)
- Approval step before production deploy

**This is the aspirational example.** Users see this and think "Carve can be my whole platform."

**Repo size:** larger; ~50 model files. Realistic.

## Consistent quality bar

Each example must:

- **Run cleanly on Snowflake trial.** No assumptions about warehouse size, schema permissions, or pre-existing tables. `carve build` from clean state must succeed.
- **Have a screenshot in README.** Show the Carve workbench mid-run.
- **Have a 90-second video.** Recorded walkthrough linked from README.
- **Pass `carve doctor`.** Each example is itself a Carve project that doctor approves.
- **Be tested in CI.** GitHub Actions runs `carve build --plan-only` on a fork weekly; nightly run on a real Snowflake test account.

## Sample data strategy

Tension: realistic data is large; repos should be small.

- Examples 1 and 2: data is in the repo (CSVs, < 5MB total, gitignored from production runs but checked in for examples).
- Example 3: data generation script (`scripts/seed.py`) creates synthetic data on demand. Run once at setup. ~50MB generated locally, never committed.

## CI harness for examples

Each example repo has `.github/workflows/carve-test.yml`:

```yaml
name: Carve example test
on:
  schedule: [{ cron: "0 6 * * 1" }]  # weekly
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install carve
      - run: carve doctor
      - run: carve build --plan-only --pipeline daily_revenue
        env:
          SNOWFLAKE_ACCOUNT: ${{ secrets.SF_ACCOUNT }}
          # ... etc
```

Failures here are surfaced loudly; a broken example is worse than no example.

## Documentation links

The main Carve docs site references each example at the appropriate point:
- "Getting started" → example 1
- "Bringing Carve to an existing dbt project" → example 2
- "Building a full data platform" → example 3

## Acceptance criteria

- [ ] Three example repos exist on GitHub under `carve-org/`
- [ ] Each has README with quickstart that takes ≤10 minutes
- [ ] Each runs end-to-end on a Snowflake trial
- [ ] Each has a 90-second walkthrough video linked from README
- [ ] Each has CI that exercises `carve build --plan-only` weekly
- [ ] All three are referenced from the main Carve docs site

## Files this spec produces

Three separate repositories, each with its own structure. In the main Carve repo:
- `docs/examples/index.md` — landing page that lists and contextualizes the three
- `docs/examples/ecommerce.md`, `brownfield.md`, `data-platform.md` — deep-dives

## What this enables

- A new user can be productive in under 30 minutes
- We have ground truth for "does Carve actually work" — broken examples = broken Carve
- Sales/marketing/community have concrete things to point at
- Each example is also a fixture for integration tests in core
