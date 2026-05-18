# Carve — Positioning shift (draft, 2026-05-14)

> Draft. Supersedes the relevant parts of `PRD.md`, `ARCHITECTURE.md`, and `PROJECT_PLAN.md` if accepted. Not yet promoted to canonical.

## The shift in one sentence

Carve is an **OSS agent-driven authoring and execution layer that builds and runs pipelines on top of dlt + dbt**, with **hosting and the production UI as the paid product**.

## Why this changes things

The previous positioning had Carve generating bespoke Python ingest code and writing its own thin runtime. Both of those mean inheriting huge surface areas — schema inference, incremental state, type coercion, retries, file rotation, parallelism — that two excellent OSS projects already solve.

Standing on **dlt** for ingest and **dbt** for transform deletes that work from the roadmap and lets us concentrate the entire team on the parts that are actually our wedge: *agents that author across the whole pipeline, an opinionated boring runtime, and a UX that ties them together.*

## Positioning vs. the obvious alternatives

| Competitor | Their wedge | Our differentiation |
|---|---|---|
| **dltHub Pro** | Hosted scheduling + observability + agentic scaffolding for *dlt pipelines* | Cross-cutting: ingest + transform + tests + deploy in one prompt, dbt-native, not ingest-only |
| **dbt Cloud** | Hosted dbt with IDE, scheduling, semantic layer | We cover ingest too; AI-native authoring; PR-based promotion flow |
| **Dagster+ / Prefect Cloud** | General-purpose orchestrators with cloud control plane | We are deliberately narrow (scheduled dbt + dlt only); simpler mental model; agent-authored, not hand-coded |
| **Airbyte / Fivetran** | Pre-built connectors + managed runtime | We don't compete on connector breadth; we generate dlt code that targets dlt's existing source library |

The bet: cross-cutting authoring + a narrow runtime is a single coherent product that none of these can ship without a strategy break.

## The OSS / Paid split

**Open source (Apache 2.0):**
- The agent layer (plan/build/refine, dbt + dlt code generation, convention inference, schema retrieval skills)
- The runtime: scheduler, job table, worker, retries, structured logs, basic alerting (Slack/email webhooks)
- The CLI
- A **minimal local web UI**, analogous to `dbt docs serve`: run history, per-run logs, basic lineage view, manual trigger. Static-ish where possible (cheap to host alongside the runtime); no multi-user, no auth beyond a local token.
- The plan → build → run → deploy lifecycle, including **PR-based deploy** for self-host

**Paid (hosted product):**
- Managed runtime (we run the scheduler/workers; users don't operate infra)
- Multi-user collaboration, SSO, RBAC
- Polished cloud UI with deeper observability (lineage, cost, freshness, anomaly callouts)
- **Push-button deploy with audit log** (alongside PR-based deploy, both supported)
- Integrations (PagerDuty, Datadog, etc.) and enterprise alerting
- Role-based plan approval, environment promotion workflows
- Hosted secrets management
- Usage-based billing on agent runs and execution minutes

**Principle:** the OSS version is a complete product for a single-team self-hoster. The paid version is what teams pay for when they don't want to operate the thing themselves and need the collaboration/governance layer. The OSS/paid line mirrors dbt-core vs dbt Cloud.

## Source strategy

How users get ingest connectors:

1. **Agent generates native dlt sources** for the user's specific API. Primary path. dlt's `@dlt.source` / `@dlt.resource` decorators are the framework target.
2. **dlt's REST API generic config-driven source** for simple REST APIs. The agent emits a config block rather than Python. Probably the 80% case.
3. **A curated Carve library of native dlt sources** — high-quality, Carve-maintained, for the most-used SaaS APIs (Stripe, Salesforce, Shopify, HubSpot, etc.). Includes ports of popular Airbyte sources rewritten as native dlt. This is both a differentiator (better DX than agent-generated code for common cases) and an ecosystem flywheel.
4. **Singer/Airbyte tap wrappers** as a fallback for obscure sources, via dlt's existing Singer protocol bridge. Inherits the upstream tap's footprint; only used when (1)-(3) don't apply.

Carve does **not** maintain its own connector framework. The framework is dlt's. We contribute upstream where it makes sense.

## Licensing approach

Following the dbt Labs / Posthog model:

- **OSS repo:** Apache 2.0. Contains everything in the open-source list above (agent, runtime, CLI, local UI).
- **Hosted repo:** separate, private. Contains the cloud-only code (multi-tenant control plane, polished web app, billing, RBAC implementation). Never published.
- **DCO** (Developer Certificate of Origin) on the OSS repo from day one. Preserves the option to dual-license later without surprising contributors.

We do **not** plan to use BSL / SSPL / Elastic License for the OSS code. Those source-available licenses are aimed at preventing AWS-scale resellers from competing with the hosted product; that's not a real risk until adoption is much larger. Reconsider if we ever see a hyperscaler-mirrored offering.

## The new architecture (sketch)

Five components, same shape as before but with different contents:

1. **Agent layer** — generates *dlt sources/resources/pipelines* and *dbt models/tests/docs*. Reads project conventions. Same plan/build/refine loop as today.
2. **Code-on-disk** — `targets/<env>/el/<artifact>/` for dlt pipelines, the user's dbt project for transforms. Code is the source of truth; git is the deploy mechanism.
3. **Runtime (new shape)** — a boring, opinionated scheduler. Job table in SQLite/Postgres, scheduler loop, worker process. Workers shell out to `dlt pipeline run` and `dbt build`. Retry-with-backoff, structured logs, run lineage, Slack/email alerts. **Explicitly not** a general-purpose DAG framework.
4. **CLI + Local UI** — primary surface for OSS users. Plan/build/run/deploy verbs plus a run history view.
5. **Hosted control plane (paid)** — multi-tenant FastAPI + Postgres + worker fleet + cloud UI. Same agent/runtime modules running in a managed environment.

### What the runtime is and isn't

- **Is:** scheduled runs of named pipelines, each pipeline a small DAG of `dlt`, `dbt`, `sql`, `shell`, or `http` steps; retry-with-backoff; structured per-run logs; an alert when a run fails; a lineage record of which dlt sources fed which dbt models.
- **Isn't (for now):** fan-out parallelism beyond intra-pipeline step parallelism, cross-pipeline conditional triggers, backfills as a first-class concept, asset-graph reactivity, custom Python operators outside the supported step types.

If users hit the wall on those, the answer is "use a real orchestrator alongside Carve" — not "we'll add it." We may add some of these post-v1, but each one we add eats the simplicity dividend.

## What changes in the existing 4-pillar plan

| Pillar | Old shape | New shape | Magnitude |
|---|---|---|---|
| **P1 Extract & Load** | Generate bespoke Python EL scripts. Carve owns ingest mechanics. | Generate **dlt pipelines** (sources, resources, `pipeline.run()`). Carve owns the *authoring* and the *deploy*, dlt owns the *mechanics*. | **Large rewrite** of P1 specs. Most "schema inference / incremental / retries" surface area deletes. |
| **P2 Transform** | dbt agent generates and modifies dbt models, tests, docs. | Essentially unchanged. Possibly tightened by `dbt` becoming a step type the runtime invokes natively. | **Small.** Mostly preserved. |
| **P3 Pipeline** | Multi-step pipelines: `el://`, `dbt://`, `sql://`, `shell://`, `http://`. | Same shape; `el://` step now invokes a dlt pipeline. Step type surface simplifies because dlt and dbt do their own thing inside. | **Medium.** Step semantics shift, but the model survives. |
| **P4 Schedule & Execution** | Sketched as "Carve provides scheduling OR generates CI snippets, user provides runtime." Optional, deferred. | **Becomes the runtime product.** Scheduler daemon, job table, worker, retries, alerts, run history. First-class, not afterthought. Web UI ships alongside. | **Largest expansion.** This is a real net-new module that the old plan barely scoped. |

**Pillar order changes.** The new sequence:

1. **P1 — Extract & Load** (dlt-based authoring)
2. **P4 → P2 — Runtime** (scheduler, jobs, worker, alerts, run history, local UI) — promoted from last to second
3. **P2 → P3 — Transform** (dbt agent)
4. **P3 → P4 — Pipeline** (multi-step composition of EL + dbt + ad-hoc)

The runtime moves up because it's the paid wedge — we can't ship the hosted product without it, and dogfooding it early against P1 dlt artifacts gives us a real test. Transform follows once we have a place to schedule it. Multi-step pipelines come last because they presume both EL and dbt are working.

## What's deliberately preserved from the current PRD/Architecture

- **Plan → build → run → deploy lifecycle**, including the config-hash drift check.
- **Code is the source of truth, UI is the editor**. Every UI edit becomes a git commit.
- **PR-based deploy for OSS self-host.** Direct apply for the hosted product.
- **Targets (`dev`, `prod`) with per-target `targets/<env>/` folders.** Still the dev/prod boundary.
- **Convention inference and "reads your conventions."** Still core to the agent.
- **Skills layer + MCP both directions.** Still the way agents do things.
- **SQLAlchemy for state, SQLite for OSS, Postgres for hosted.** Same seam.

## Implications for things outside the four pillars

- **Snowflake-only stance loosens.** dlt is destination-agnostic, and so is dbt. We can still ship Snowflake-first for v0.1 (focus, fewer test matrices) but the architectural commitment to *only* Snowflake softens. Postgres/BigQuery/Databricks become P5+ work that adds adapter glue, not a rewrite.
- **The "Carve owns the execution layer" decision (PRD §5.2) is now more defensible**, because the layer is genuinely narrow (scheduled dbt + dlt) rather than a general process runner. Worth rewriting that section.
- **The "we are not Dagster" message is now load-bearing**. Documentation needs an explicit "when to pick Carve vs Dagster/Prefect" page so users self-select correctly.
- **Quality agent (P4 in old plan)** can probably fold into the dbt agent. dlt's schema contracts cover the ingest-side quality story.

## Decisions (resolved 2026-05-14)

1. **OSS gets a minimal local UI** (dbt-docs-shaped: run history, logs, lineage, manual trigger). Full operational/cloud UI is paid-only.
2. **Source strategy is layered**: agent generates native dlt + dlt REST API generic source, plus a curated Carve library of native dlt sources (including ports of popular Airbyte sources). Singer/Airbyte wrapper is a fallback for the long tail. See *Source strategy* section above.
3. **Pillar order changes** to P1 (dlt EL) → P2 (runtime) → P3 (dbt transform) → P4 (multi-step pipeline). Runtime promoted from last to second.
4. **Relationship with dltHub is mostly friendly.** We generate their code and drive adoption; we differ in that we cover the whole warehouse lifecycle in one place, not just ingest. Avoid framing dltHub as a competitor in public communication; consider a partnership conversation once we have traction.
5. **Both deploy paths are supported.** PR-based deploy is the OSS path; the hosted product adds push-button deploy with audit log alongside it.
6. **Licensing**: Apache 2.0 OSS repo + private hosted repo, dbt Labs model. See *Licensing approach* section above. No BSL/SSPL.

## Decisions (resolved 2026-05-14, round 2)

7. **Curated source library lives in the main OSS monorepo** (e.g. `carve/sources/`). Reconsider extraction once the repo gets unwieldy or release cadences diverge. Quality bar matches the rest of Carve (typed, tested, documented); upstream contribution to `dlt-hub/verified-sources` is fine on a case-by-case basis but not the primary destination.
8. **Initial Airbyte port list is deferred.** Heuristic when the time comes: "most popular Airbyte sources that dlt doesn't already have native coverage of," informed by what real users tell us they want to ingest. Not a v0.1 blocker.
9. **OSS local UI is static HTML regenerated per run**, modeled on `dbt docs`. At the end of each run the runtime re-renders `index.html` (run history table, lineage view, links to log files on disk). No live updates, no interactivity beyond links, no auth. Strong upgrade hook to the hosted product, which provides live operational monitoring. Honest trade: feels dated, but limited *by design*.
## Decisions (resolved 2026-05-14, round 3)

10. **OSS runtime supports multi-worker from day one.** Optimistic-claim job table (`UPDATE jobs SET claimed_by = ?, status = 'running' WHERE id = ? AND status = 'queued'`), per-worker heartbeats so a crashed worker's job can be reclaimed, per-pipeline serialization so a pipeline doesn't race itself. Configurable worker count (default likely 1–2 for OSS, much higher for hosted). The OSS/paid concurrency story shifts: paid is no longer "you get parallelism" but "you get managed parallelism + larger worker fleets + fairness/quota tooling."

11. **Postgres is the state store from day one.** Supersedes PRD §5.9 ("SQLite first, Postgres later"). Driven by the multi-worker decision — SQLite write contention under N concurrent workers is a real ceiling we'd hit fast. Side benefit: the OSS-to-hosted migration gets cheaper (no SQLite→Postgres rewrite).

12. **OSS ships a bundled docker-compose for default install + supports external Postgres for production.** Same pattern as Sentry / Posthog / Gitea / Mattermost / Discourse. First-run experience is `git clone carve && docker compose up` (or equivalent), with Postgres included as a service in the compose file. Production users can override the connection string to point at managed Postgres (RDS, Cloud SQL, Supabase, etc.). Docker becomes a soft dependency for the easy path; CLI-only / non-Docker installs require external Postgres.

13. **Carve is headless by default. The full REST API and MCP server are OSS.** Every action available to Carve's own agents is available to external agents, chat tools, and scripts via REST or MCP. The CLI and local static-HTML UI are just two clients among many; users can also drive Carve from Claude Desktop, Cursor, Claude Code, or custom agents. The hosted product's moats are *operational, not feature-functional* — we do not gate API endpoints. The hosted product adds multi-tenancy, SSO/OAuth/RBAC, service accounts, audit log, rate limiting, polished cloud UI, premium integrations, hosted secrets, and managed infra; it does not add API capabilities the OSS version lacks. This is the dbt Labs / Sentry / Posthog model and explicitly rejects the open-core gating anti-pattern.

14. **v0.1 bundles Pillars 1, 2, and 4 (dlt EL agent + runtime + multi-step pipeline composition) into a single release.** P3 (dbt agent) is the only pillar deferred to v0.2. Rationale: shipping P1 + P2 without P4 would leave v0.1 users unable to sequence dlt → dbt as a single pipeline — they'd have to schedule them as two separate cron jobs at staggered times, which is fragile and surprising for the headline use case. Bundling P4 in v0.1 makes the first release demonstrate the *full Carve loop* (intent → plan → build → run → deploy → schedule a composed pipeline → observe) end to end, even though dbt models are still user-authored until v0.2 ships the dbt agent. Note this collapses the version cadence: PROJECT_PLAN.md needs to handle that v0.2 carries only the dbt-agent pillar.

## Still open

- **Migration from M1's existing SQLite state store.** The walking skeleton (already shipped) uses SQLite. New direction is Postgres. Cleanest path is probably "wipe and start over with Postgres" since v0.1 isn't released yet, but worth confirming.
- **Static-HTML UI implementation detail**: pure file regeneration, or a tiny read-only HTTP server that reads from the state store? Probably pure file regen for simplicity (`carve docs serve` to view), but a 50-line FastAPI app reading SQLite isn't much harder. Decide at implementation time.
- **How the worker interacts with `carve run <pipeline>` invoked manually while the scheduler is also running.** Does the manual invocation jump the queue, queue normally, or run out-of-band of the worker entirely? Probably "queue normally and surface running state in CLI" is right, but worth confirming.

## Next step

If this shape is right, the path is:
1. Walk the existing 43 specs against it. Tag each as `KEEP`, `REVISE`, `REWRITE`, or `DELETE`.
2. Rewrite `PRD.md`, `ARCHITECTURE.md`, `PROJECT_PLAN.md` to match. Archive the originals.
3. Re-sequence the pillars (P1 dlt, P4 runtime, P2 dbt, P3 pipeline — or whatever order survives review).
4. Draft a positioning page for the docs site (when there is one) covering "Carve vs dltHub Pro / dbt Cloud / Dagster".

If the shape is *not* right, the cheap thing is to fix this document before any of the above starts.
