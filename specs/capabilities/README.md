# Carve — capability specs

The durable, **version-independent** design of each Carve capability — the lowest level of design detail (per [`../_strategy/2026-06-spec-structure.md`](../_strategy/2026-06-spec-structure.md)). Each file describes *how a capability works*, with phasing expressed as annotations ("column-level lineage is a later increment"). **Sequencing and "what to build when" live in [`../DELIVERY.md`](../DELIVERY.md), not here.**

## The capabilities

**Foundation**
- [`state-store.md`](./state-store.md) — Postgres state store (SQLite retired)
- [`packaging.md`](./packaging.md) — bundled docker-compose Postgres + external-Postgres option
- [`layout.md`](./layout.md) — control-plane `carve.toml`, `[components.<name>]`, the component locator, repo topology, simple-mode discovery
- [`harness.md`](./harness.md) — the AI harness: subagent delegation, terminal tools, the permission gate, verify-by-execution
- [`extensibility.md`](./extensibility.md) — declarative agents, skill packs, hooks, MCP (both directions)

**Components & composition**
- [`sql.md`](./sql.md) — the dialect-aware SQL tool layer + thin specialist
- [`dlt-engineer.md`](./dlt-engineer.md) — the DLT engineer subagent (+ dlt-qa / dlt-security reviewers)
- [`pipelines.md`](./pipelines.md) — pipeline composition, the step DAG, `[seed_schedule]`, the pipeline engineer

**Runtime & bootstrap**
- [`runtime.md`](./runtime.md) — scheduler, job queue, workers, reaper, archiver, the live `schedules` table
- [`init.md`](./init.md) — `carve init` (greenfield / brownfield)
- [`memory.md`](./memory.md) — conventions / standards / decisions + `carve memory`

**Interfaces & investigation**
- [`rest-api.md`](./rest-api.md) — the FastAPI surface (CLI parity, auth, streaming, webhooks)
- [`mcp-server.md`](./mcp-server.md) — the MCP adapter over REST
- [`ui.md`](./ui.md) — the static HTML UI
- [`ask.md`](./ask.md) — the explorer (`carve ask`)
- [`lineage.md`](./lineage.md) — lineage by investigation (no Carve store)

**Deploy & recovery**
- [`deploy.md`](./deploy.md) — `carve deploy` configurable handoff + cross-repo linked PRs
- [`recovery.md`](./recovery.md) — the recovery engineer (diagnose-then-delegate)

**Docs**
- [`reference-docs.md`](./reference-docs.md) — keeping the reference docs ([`../reference/`](../reference/)) in lock-step

## Notes

- These are **design** references. For the dependency-ordered build sequence, current state, and per-increment scope, see [`../DELIVERY.md`](../DELIVERY.md).
- The harness specs ([`harness.md`](./harness.md) + [`extensibility.md`](./extensibility.md)) are the AI foundation everything else runs on.
- Landed M1 / M1.1 follow-up work orders are archived in [`../_archive/`](../_archive/).

## Cross-references

- Product requirements: [`../PRD.md`](../PRD.md)
- Architecture: [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
- Delivery plan: [`../DELIVERY.md`](../DELIVERY.md)
- Strategy / ADRs: [`../_strategy/`](../_strategy/) (control-plane, AI-harness, spec-structure)
