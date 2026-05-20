# Carve v0.1 — spec set

13 specs that compose Carve's first formal release. v0.1 bundles Pillars 1, 2, and 4 (per [`../PROJECT_PLAN.md`](../PROJECT_PLAN.md)). v0.2 will add Pillar 3 (dbt agent) in a separate spec set.

All v0.1 code is implemented by Claude Code via the `/build-spec` workflow (per the *Implementation approach* section of `PROJECT_PLAN.md`). Each spec below is one `/build-spec` iteration: dependency check → phase plan → engineer → reviewer fan-out → fix iterations → spec-keeper sync → PR open for review.

## Specs in implementation order

| #  | Spec                                                                | Theme                                                    | Status   |
|----|---------------------------------------------------------------------|----------------------------------------------------------|----------|
| 01 | [`01-state-store-postgres.md`](./01-state-store-postgres.md)        | State store: Postgres only (SQLite retired)                            | Landed (partial; sweep deferred to 01-followup) |
| 01b | [`01-followup-m1-test-sweep.md`](./01-followup-m1-test-sweep.md)   | M1 test fixture sweep + missing v0.1-01 unit tests                     | Landed |
| 01c | [`01-followup-database-url-env-precedence.md`](./01-followup-database-url-env-precedence.md) | Native `DATABASE_URL` precedence in `resolve_state_store_url` (collapses the `cli_env` shim) | Landed (2026-05-20) |
| 02 | [`02-oss-packaging.md`](./02-oss-packaging.md)                      | Bundled docker-compose with Postgres; external-Postgres option        | Drafting |
| 03 | [`03-flat-layout.md`](./03-flat-layout.md)                          | Flat `el/<name>/` layout for dlt artifacts; per-backend repo topology | Drafting |
| 04 | [`04-el-agent-dlt.md`](./04-el-agent-dlt.md)                        | EL specialist agent generates dlt code (native, REST API config, curated library, MCP wrapper) | Drafting |
| 05 | [`05-init-rewrite.md`](./05-init-rewrite.md)                        | `carve init` for greenfield/brownfield dlt+dbt; scaffolds memory      | Drafting |
| 06 | [`06-project-memory.md`](./06-project-memory.md)                    | `carve/{conventions,standards,decisions}.md`, per-pipeline sidecars, `carve memory *` | Drafting |
| 07 | [`07-runtime.md`](./07-runtime.md)                                  | Scheduler, job table, optimistic claim, workers, heartbeats, reaper   | Drafting |
| 08 | [`08-multi-step-pipeline.md`](./08-multi-step-pipeline.md)          | Pipeline TOML schema, step DAG executor, `dlt`/`dbt`/`sql` step types, failure modes | Drafting |
| 09 | [`09-rest-api.md`](./09-rest-api.md)                                | FastAPI app with full coverage of CLI surface; auth, errors, pagination, streaming, webhooks | Drafting |
| 10 | [`10-mcp-server.md`](./10-mcp-server.md)                            | MCP server auto-generated from REST endpoints; stdio + WebSocket transports | Drafting |
| 11 | [`11-static-html-ui.md`](./11-static-html-ui.md)                    | Jinja templates regenerated on run completion; `carve docs serve`     | Drafting |
| 12 | [`12-ask-verb.md`](./12-ask-verb.md)                                | Read-only `carve ask` verb with no-write-skill guardrail              | Drafting |
| 13 | [`13-reference-docs.md`](./13-reference-docs.md)                    | `cli-reference.md`, `config-schema.md`, `glossary.md`, `governance.md` rewrites | Drafting |

## Status legend

- **TBD** — spec not yet drafted
- **Drafting** — spec being written
- **Ready** — spec complete; ready for `/build-spec`
- **Building** — `/build-spec` in flight
- **In review** — PR open against this repo
- **Landed** — merged
- **Blocked** — depends on another v0.1 spec that isn't done

## Cross-references

- Foundational shape and decisions: [`../_strategy/2026-05-positioning.md`](../_strategy/2026-05-positioning.md)
- Pre-rewrite audit (REVISE / REWRITE / DELETE per existing spec): [`../_strategy/spec-audit.md`](../_strategy/spec-audit.md)
- Product requirements: [`../PRD.md`](../PRD.md)
- Architecture: [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
- Project plan: [`../PROJECT_PLAN.md`](../PROJECT_PLAN.md)
- Old in-flight specs (now archived; some of their content is carried forward into v0.1 specs above): [`../_archive/pillar-1-extract-load/`](../_archive/pillar-1-extract-load/), [`../_archive/pillar-1.1-flat-layout/`](../_archive/pillar-1.1-flat-layout/)
