# Carve

**Agent-native data engineering, from natural language to production pipelines.**

Carve takes a goal in plain English ("make `stg_orders` incremental", "ingest the Stripe charges API into a Snowflake table"), turns it into a reviewable plan, and — when you approve — opens a pull request in your repo with the working code. dbt models, Snowflake DDL, Python ingest scripts, tests, schema docs. All the boring parts of building and maintaining a warehouse pipeline, executed by agents that read your conventions and respect your existing project.

> **Status: under active development.** Carve is pre-alpha. The specs in [`specs/`](./specs/) describe what's being built. The first usable release is `v0.1.0`, planned for end of milestone 3. Expect breaking changes until then.

## Setup

`carve init` scaffolds a project, including a `.env.example`. Copy it to
`.env`, fill in the values, and run any `carve` command from the project
root — the CLI auto-loads `.env` on startup, so there's no
`set -a; source .env; set +a` ritual. Existing shell vars take precedence
over `.env`. Set `CARVE_NO_DOTENV=1` to disable the auto-load if you
manage env vars elsewhere (direnv, mise, 1Password CLI).

## Where to look

- **[`specs/`](./specs/)** — the 43 design specs that drive every line of code in this repo. Start with [`specs/PROJECT_PLAN.md`](./specs/PROJECT_PLAN.md) for the milestone roadmap, then [`specs/PRD.md`](./specs/PRD.md) for product context and [`specs/ARCHITECTURE.md`](./specs/ARCHITECTURE.md) for system design.
- **[`OSS_COMMITMENT.md`](./OSS_COMMITMENT.md)** — what is permanently open source vs. what we reserve for a future commercial offering.
- **[`CONTRIBUTING.md`](./CONTRIBUTING.md)** — DCO sign-off, dev setup, PR expectations.

## License

Apache 2.0. See [`LICENSE`](./LICENSE).
