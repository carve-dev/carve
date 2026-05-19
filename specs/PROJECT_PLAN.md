# Carve — Project Plan

> Last major revision 2026-05-19, aligned to [`_strategy/2026-05-positioning.md`](./_strategy/2026-05-positioning.md), [`_strategy/spec-audit.md`](./_strategy/spec-audit.md), [`PRD.md`](./PRD.md), and [`ARCHITECTURE.md`](./ARCHITECTURE.md). For the prior version, see [`_archive/PROJECT_PLAN-pre-2026-05-positioning.md`](./_archive/PROJECT_PLAN-pre-2026-05-positioning.md).

## The shape of Carve

Carve is **four product pillars** plus a separately-tracked **hosted product**. Pillars 1, 2, and 4 ship together in v0.1; Pillar 3 ships as v0.2; the hosted product runs on a parallel timeline.

This is a change from the original four-version-per-pillar plan, driven by the 2026-05 positioning shift (positioning decision #14): Carve's first release needs the runtime to demonstrate the full pitch (agent + runtime + composed pipelines), and shipping P1 alone — "an AI that writes dlt code" — would leave the first release feeling incomplete and indistinguishable from dltHub's agentic scaffolding.

## The four pillars

| Pillar | Theme                    | Ships in | Status              |
|--------|--------------------------|----------|---------------------|
| **P1** | Extract & Load           | v0.1     | Spec rewrite needed |
| **P2** | Runtime                  | v0.1     | Largely net-new     |
| **P3** | Transform (dbt agent)    | v0.2     | Planned             |
| **P4** | Multi-step pipeline      | v0.1     | Largely net-new     |

**The hosted product** is a separately-tracked release. It depends on a stable v0.1 OSS and adds: multi-tenancy, SSO/OAuth/RBAC, audit log, push-button deploy, polished cloud UI, premium integrations, hosted secrets, billing. Some hosted work can happen in parallel with v0.2 once v0.1 OSS stabilizes.

## Foundation (already shipped)

Two pre-pillar milestones laid the groundwork:

- **M1 — Walking skeleton.** Smallest end-to-end loop: CLI foundation, config loader, state store, Anthropic agent loop with tool-use, Python step + `LocalVenvRunner`, Snowflake connector. Specs in [`../milestone-1-walking-skeleton/`](../milestone-1-walking-skeleton/). **Shipped.**
- **M1.1 — Follow-ups.** UX polish and the pipeline-centric lifecycle: init config templates, Claude Code OAuth path, dotenv autoload, live progress output, plan-prompt tightening, plan/build/run separation, run-retry-permits-redo. Specs in [`../milestone-1.1-followups/`](../milestone-1.1-followups/). **Shipped.**

Combined, M1 + M1.1 give Carve the agent loop, state store, runner, connector, OAuth, and the `plan → build → run → deploy` lifecycle that all four pillars build on. ~300 tests passing.

**Code-revision implications under the new positioning.** Per the spec audit, two M1 components need code revision before v0.1:

- **M1-03 state store** — currently SQLAlchemy + SQLite; needs to migrate to Postgres-from-day-one. A one-shot `carve migrate-state --from sqlite --to postgres` tool ships in v0.1 for the small set of existing walking-skeleton users.
- **M1-05 Python step + `LocalVenvRunner`** — survives as a worker subprocess primitive. The new P2 runtime wraps it with scheduler + job queue + multi-worker semantics; M1-05 doesn't get removed, just gets a layer above it.

All other M1 and M1.1 specs are HISTORICAL (the code is what shipped) and require no further action.

## Guiding principles

- **Ship before perfect.** The version that gets feedback in week 2 is more valuable than the one that ships in month 6 with three more features.
- **Pick boring technology.** `typer`, `pydantic`, `SQLAlchemy`, `Postgres`, `Anthropic SDK`, `tomlkit`, `dlt`, `dbt-core`. Save the novelty budget for the agent layer.
- **OSS feature-complete; hosted operationally distinct.** No API endpoints or MCP tools gated behind hosted (positioning decision #13). Hosted earns its price on operational excellence, not feature exclusivity.
- **Ship pillars together when they need each other.** v0.1 bundles P1 + P2 + P4 because without the runtime and multi-step composition, P1 alone is too narrow to demonstrate Carve's pitch.
- **Defer extension points.** Hard-code built-in skills, agents, and step types until they've stabilized. Custom skill SDK and custom step type SDK both ship post-v0.1.
- **dlt and dbt are external backends, not internal modules.** We generate code that uses them; we do not reimplement them.
- **Revise plans, not code.** Catching strategic shifts at the spec level is cheap; catching them in shipped code is expensive. Multiple rounds of spec revision are healthy, not waste.

## Implementation approach

**All v0.1 code is written by Claude Code via the `/build-spec` workflow.** Nate is the reviewer and director — strategy, specs, PR review, merge gate — not the hands-on engineer. The plan/build/PR cycle that Carve provides for its users is also how Carve itself is built; PRs land in this repo, get reviewed, merge.

This has two practical consequences for how specs are written:

1. **Specs must be complete and self-contained.** Claude Code does what the spec says — there is no human engineer filling in gaps with judgment. Ambiguity costs review cycles, not engineer time. Spec quality is the leverage point.
2. **Elapsed time is gated by spec rigor and review pace**, not engineer-weeks. A well-specified pillar lands faster than a poorly-specified one, regardless of how big it is in lines of code.

The `/build-spec` skill (built-in to this repo) is the primary implementation mechanism. Each spec gets implemented in its own iteration: dependency check, plan, engineer, reviewer fan-out (Python, dbt, Snowflake, security, QA depending on the spec), fix iterations, then spec-keeper sync. The orchestration agent and its sub-agents drive all of this; Nate's job is to write good specs and to approve good PRs.

## v0.1 — agent + runtime + multi-step (in flight)

**Theme.** A self-hoster with a Snowflake account (and possibly an existing dbt project) can describe what they want, get a working pipeline, and run it on a schedule — all in one Carve install. Multi-step pipelines composing dlt + dbt + sql execute end-to-end.

**Acceptance criteria.** A data engineer can:

1. Run `carve init` — gets `carve.toml`, `carve/` (including memory scaffolding: `conventions.md` populated from inference, plus empty-with-template `standards.md` and `decisions.md`), `el/`, `pipelines/`, `docker-compose.yml` (bundled Postgres), and `.dlt/` config templates. Detects existing dbt and/or dlt projects in same-repo mode, or accepts `--dbt-path/--dlt-path/--dlt-url` for separate-repo mode (PRD §6.1, §6.2, §6.3).
2. Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` (or run `carve auth login` for OAuth) + target credentials.
3. `docker compose up` brings up Carve + Postgres on the local machine.
4. `carve plan "ingest the Stripe charges API and stage it in raw_stripe"` — agent produces a reviewable plan with file diffs and cost estimate.
5. `carve build <plan_id>` — agent writes dlt pipeline files into `el/stripe_charges/` plus a `pipelines/stripe.toml` composing the dlt step + a stub dbt step.
6. `carve run stripe --target dev` — runs the composed pipeline against dev Snowflake; rows land.
7. `carve deploy stripe` — opens a PR with the new files.
8. Merge the PR; `carve serve` running in prod picks up the pipeline based on its cron schedule and fires runs automatically.
9. Failed runs surface in the local static-HTML UI and via Slack/email webhooks declared in `runtime.toml`.

**Investigative companion.** `carve ask "where do we calculate net revenue?"` answers questions read-only without producing a plan (PRD §6.5). When the question is "why did we decide X?", `ask` cites `carve/decisions.md` entries (PRD §6.3).

**External-driver acceptance.** A Claude Desktop user registers Carve's MCP server and runs the same loop above by chatting with Claude. A CI workflow calls the REST API to plan/build/deploy from a GitHub Action.

**Spec rewrite needed.** The 23 in-flight Pillar 1 and Pillar 1.1 specs all need REVISE, REWRITE, or DELETE per the spec audit. The biggest single rewrite is P1-04 (extract-load agent), whose premise — agent authors bespoke Python with `executemany` / `MERGE` — is broken under the new positioning. The runtime specs (P2) and the multi-step composition specs (P4) are largely net-new and will be drafted fresh.

**Spec set, in rough implementation order** (per the audit's suggested order, adjusted for the v0.1 bundling):

1. **State store migration** — bump the M1 SQLAlchemy state to Postgres-from-day-one; ship the one-shot `carve migrate-state` tool
2. **OSS packaging** — bundled `docker-compose.yml` with Postgres; `carve init --external-postgres` path
3. **Layout** (P1-01 + P1.1-01 revisions) — flat `el/<name>/` for dlt artifacts; per-backend repo topology in `carve.toml`
4. **EL agent rewrite** (P1-04) — agent generates dlt code; chooses among native dlt source, REST API config, curated library, MCP-wrapped Singer/Airbyte
5. **Init rewrite** (P1-03) — bootstraps Carve + Postgres + (optional) dlt/dbt scaffolds; integrates with brownfield dlt/dbt; scaffolds memory files
6. **Project memory** (new) — `carve/{conventions, standards, decisions}.md`, per-pipeline sidecars, `carve memory *` CLI surface (PRD §6.3)
7. **Runtime** (new P2 specs) — scheduler, job table with partial unique indexes, optimistic claim, workers, heartbeats, reaper
8. **Multi-step pipeline composition** (new P4 specs) — pipeline TOML schema, step DAG executor, dlt/dbt/sql step types, failure modes
9. **REST API** — FastAPI app with full coverage of CLI surface
10. **MCP server** — auto-generated from REST endpoints (stdio + WebSocket transports)
11. **Static HTML UI** — Jinja templates + regeneration on run-completion
12. **Ask verb** — read-only orchestrator path with the no-write-skills guardrail (PRD §6.5)
13. **Reference doc rewrites** — `cli-reference.md`, `config-schema.md`, `glossary.md`, `governance.md`

The audit-derived REVISE/REWRITE/DELETE table in [`spec-audit.md`](./spec-audit.md) is the per-spec source of truth.

**Pace.** Elapsed time is gated by spec rigor and review pace, not engineer-weeks (see *Implementation approach* above). Each spec lands via one `/build-spec` iteration plus Nate's PR review; total v0.1 is roughly "however long Nate takes to write the specs and review the PRs," with Claude Code's build time as a smaller component.

**Internal milestone.** When `carve init` → plan → build → run → deploy → scheduled-run-on-cron works end-to-end against a real Snowflake account (and the same loop works via REST and MCP), tag `v0.1.0`.

## v0.2 — dbt agent (planned)

**Theme.** Add the dbt specialist. v0.1 users can already invoke dbt via the runtime (the `dbt` step type executes `dbt build` against their existing project); v0.2 lets the agent **author and modify** dbt models, tests, and `sources.yml` entries.

**Probable scope** (specs to be drafted closer to the work):

- dbt specialist agent and prompt
- dbt-aware skills (`list_models`, `model_columns`, `model_dependencies`, `tests_on_model`)
- Brownfield convention inference for dbt (already partially in v0.1 via `carve init`; v0.2 deepens it)
- Greenfield dbt scaffolding (`carve init --with-dbt` lands in v0.1 but generates dbt models lands in v0.2)
- Cross-backend source coupling: EL agent generates dlt pipelines that target sources the dbt agent declared (mostly v0.1, refined in v0.2)
- Quality patterns: agent learns from existing test patterns and generates appropriate tests on new models

Pillar 3 stays standalone in the sense that a user with no dlt artifacts can use Carve purely for dbt authoring + dbt runs against an existing project.

## The hosted product (parallel track)

**Theme.** A managed Carve that teams can subscribe to without operating Postgres, docker-compose, or workers themselves.

**Scope** (lives in the private `carve-hosted` repo):

- Multi-tenant control plane (request routing, tenant isolation, per-tenant worker pools)
- Authentication (SSO via Google/Okta/Azure AD, OAuth, service accounts, RBAC)
- Audit log (every API call recorded with actor, payload, response)
- Push-button deploy with optional plan-approval workflows
- Polished cloud UI (live monitoring, lineage view, cost dashboards, deploy approvals)
- Premium integrations (PagerDuty, Datadog, Slack with formatted payloads)
- Hosted secrets via a Vault-backed store
- Billing (usage-based metering on agent runs and execution minutes)
- Rate limiting and per-team quotas

**Timeline.** Begins after v0.1 OSS stabilizes (so the API surface is no longer churning). Can overlap with v0.2 OSS work. Early access targeted for shortly after v0.1.0; general availability targeted for shortly after v0.2.0.

The hosted repo depends on the OSS repo as a library; it does not fork the OSS code.

## Risk and slip

- **Schema retrieval edge cases.** Real-world Snowflake accounts are messier than fixtures. *Mitigation*: dogfood early against three different test projects; design the skill-categories framework (ARCHITECTURE §6.4) so partial context surfaces explicitly rather than silently corrupting agent output.
- **Brownfield detection edge cases.** dbt projects in the wild use a wide range of layouts and conventions; dlt brownfield is newer territory. *Mitigation*: the convention inference output (`carve/conventions.md`) is user-editable, so misdetections are easy to correct.
- **dlt or dbt ship breaking changes.** A real risk now that Carve depends on both. *Mitigation*: pin minor versions, test against multiple dlt/dbt versions in CI, ship a Carve patch within 2 weeks of any breaking change in our supported versions.
- **Runtime complexity** (new in v0.1, not deferred to v0.4 as in the old plan). Multi-worker semantics with optimistic claim, heartbeats, reaper. *Mitigation*: keep the runtime narrow (no general-purpose orchestration features), schema-enforce uniqueness invariants so application-level bugs can't break the queue, write integration tests that simulate crashed workers and Postgres restarts.
- **OSS and hosted code drift** as both evolve. *Mitigation*: hosted depends on OSS as a library, integration tests run against both, quarterly review.
- **Postgres-from-day-one friction.** The bundled docker-compose adds setup steps that a SQLite-backed CLI wouldn't have. *Mitigation*: the bundled compose makes first-run one command (`docker compose up`); friendly errors when Docker is missing; documented external-Postgres path for users with managed Postgres.

If a pillar slips, the slip cuts scope rather than time — defer pieces to a follow-up release. The version sequence (v0.1, v0.2, …) is more important than feature completeness within any one.

## What this plan deliberately defers

- **Multi-LLM-provider support.** Anthropic-only for v0.1. OpenAI / Google / others come when there's user demand.
- **BigQuery, Databricks, Redshift first-class.** v0.1 users can target these via dlt natively (and dbt natively, when v0.2 lands), but Carve maintainers only test against Snowflake. Elevating these adapters to first-class is a post-v0.2 effort.
- **Multi-user authentication in OSS.** Single user in OSS; multi-user happens in the hosted product.
- **In-process custom skill SDK.** v0.1 ships built-in skills + MCP-imported skills. The `@skill`-decorated Python SDK is post-v0.1 — likely v0.2 or v0.3 if real demand emerges. The MCP path remains supported indefinitely.
- **Custom step type SDK.** Built-in step types (`dlt`, `dbt`, `sql`) only in v0.1. Extension SDK lands once the three built-ins have stabilized.
- **Visual pipeline editor.** TOML authoring via agents is the primary path; CLI is the escape hatch. A visual editor would be post-hosted-launch, if at all.
- **K8s operator.** OSS users running in K8s use Helm or raw manifests; we don't ship a Carve-specific CRD.
- **dbt Cloud as a step backend.** Possibly later; stays out of v0.x.

## What's after v0.2

The first 30 days post-launch of each version are about listening, not building. The roadmap evolves with real user feedback. Early candidates for v0.3+:

- In-process custom skill SDK
- Additional step types (`shell`, `http`, `python` as a generic step)
- Embedding-based semantic schema search
- BigQuery / Databricks / Redshift first-class adapters
- Multi-LLM-provider support
- Visual pipeline monitor (if the static-HTML UI isn't enough)
- Multi-pipeline operations (e.g., "rebuild the whole staging layer")

These are guesses. Actual v0.3+ priorities come from issues, PRs, and conversations after each version ships.
