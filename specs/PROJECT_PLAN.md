# Carve — Project Plan

> Last major revision 2026-05-19, aligned to [`_strategy/2026-05-positioning.md`](./_strategy/2026-05-positioning.md), [`_strategy/spec-audit.md`](./_strategy/spec-audit.md), [`PRD.md`](./PRD.md), and [`ARCHITECTURE.md`](./ARCHITECTURE.md). For the prior version, see [`_archive/PROJECT_PLAN-pre-2026-05-positioning.md`](./_archive/PROJECT_PLAN-pre-2026-05-positioning.md).

**Carve is a control plane plus an AI harness, over independently-versioned dlt/dbt/sql components — not a project that contains them.** Per [`_strategy/2026-06-control-plane.md`](./_strategy/2026-06-control-plane.md) and [`_strategy/2026-06-ai-harness.md`](./_strategy/2026-06-ai-harness.md). The value proposition: **build, schedule, and monitor pipelines — all with AI.** Carve schedules, orchestrates, and monitors dlt + dbt + sql pipelines; its AI builds and deploys those components for you, or you bring your own and Carve just orchestrates.

The work is organized as **four product pillars** plus a separately-tracked **hosted product**. Pillars 1, 2, and 4 ship together in v0.1; Pillar 3 ships as v0.2; the hosted product runs on a parallel timeline. v0.1 bundles them because the control plane needs at least one component-engineer (DLT) + composition + the AI harness to demonstrate the full pitch — shipping "an AI that writes dlt code" alone would be indistinguishable from dltHub's agentic scaffolding.

## The four pillars

Re-articulated around the control-plane model: **P2 is the control plane**; **P1 and P3 are the components the AI builds**; **P4 is the composition that binds them**; and the **AI harness** is the cross-cutting agentic engine all of it runs on.

| Pillar | Theme | Ships in | Status |
|--------|-------|----------|--------|
| **P1** | Extract & Load — the **DLT component + engineer** (AI authors/runs dlt components) | v0.1 | Specs drafted (04) |
| **P2** | Runtime — the **control plane** (scheduler / executor / monitor that references components by name) | v0.1 | Specs drafted (07) |
| **P3** | Transform — the **dbt component + engineer** | v0.2 | Planned |
| **P4** | Multi-step pipeline — **composition** (the binding contract: components by name → step DAG) | v0.1 | Specs drafted (08) |

Underpinning all four: the **AI harness** — a Claude-Code-style agentic engine (subagent orchestration, terminal-grade tools, a permission system, verify-by-execution, and declarative agents/skills/hooks extensibility), specs 15–16, plus the recovery engineer (17) and the dialect-aware SQL tool layer (18).

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
- **Ship pillars together when they need each other.** v0.1 bundles the control plane (P2) + the DLT component-engineer (P1) + composition (P4) + the AI harness, because the control plane needs a component to build, compose, and run to demonstrate the pitch.
- **Extensibility is declarative + in v0.1.** Declarative agents (`carve/agents/*.md`), skill packs (`SKILL.md`), hooks, and MCP import ship in v0.1 (specs 15–16) — the "bring your own agents/skills/tools" foundation. Only the *in-process* custom-skill SDK and the custom *step-type* SDK are deferred post-v0.1 (the MCP + `SKILL.md` paths cover v0.1).
- **dlt and dbt are external backends, not internal modules.** We generate code that uses them; we do not reimplement them.
- **Revise plans, not code.** Catching strategic shifts at the spec level is cheap; catching them in shipped code is expensive. Multiple rounds of spec revision are healthy, not waste.

## Implementation approach

**All v0.1 code is written by Claude Code via the `/build-spec` workflow.** Nate is the reviewer and director — strategy, specs, PR review, merge gate — not the hands-on engineer. The plan/build/PR cycle that Carve provides for its users is also how Carve itself is built; PRs land in this repo, get reviewed, merge.

This has two practical consequences for how specs are written:

1. **Specs must be complete and self-contained.** Claude Code does what the spec says — there is no human engineer filling in gaps with judgment. Ambiguity costs review cycles, not engineer time. Spec quality is the leverage point.
2. **Elapsed time is gated by spec rigor and review pace**, not engineer-weeks. A well-specified pillar lands faster than a poorly-specified one, regardless of how big it is in lines of code.

The `/build-spec` skill (built-in to this repo) is the primary implementation mechanism. Each spec gets implemented in its own iteration: dependency check, plan, engineer, reviewer fan-out (Python, dbt, Snowflake, security, QA depending on the spec), fix iterations, then spec-keeper sync. The orchestration agent and its sub-agents drive all of this; Nate's job is to write good specs and to approve good PRs.

## v0.1 — agent + runtime + multi-step (in flight)

**Theme.** **Build, schedule, and monitor pipelines — all with AI.** A self-hoster with a Snowflake account (and possibly an existing dbt project) describes what they want; Carve's AI harness builds the dlt component, composes a multi-step pipeline (dlt + dbt + sql), and the control plane schedules + runs + monitors it — all in one install. Or they bring existing dlt/dbt and Carve just orchestrates (orchestration-only mode).

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

**Spec status.** The v0.1 spec set is drafted and revised to the control-plane + AI-harness model; the original Pillar 1/1.1 specs were archived and their content carried forward. The foundation harness specs (15 agent-harness, 16 extensibility) have been adversarially reviewed and hardened.

**Spec set** (full list + per-spec status in [`v0.1/README.md`](./v0.1/README.md) — 19 specs), grouped:

- **Foundation:** 01 state-store (Postgres-only), 02 OSS packaging, 03 control-plane layout (`carve.toml` + `[components.<name>]`), 15 agent-harness, 16 extensibility.
- **Components + composition:** 04 DLT engineer, 08 multi-step pipeline composition, 18 SQL tool layer.
- **Control plane / runtime + bootstrap:** 07 runtime (scheduler / workers / reconciler), 05 init, 06 project-memory.
- **Interfaces:** 09 REST API, 10 MCP server, 11 static-HTML UI, 12 explorer (`ask`), 19 lineage (investigate dbt/dlt native lineage on demand; no Carve store).
- **Deploy + recovery:** 14 deploy (configurable handoff + linked-PR), 17 recovery engineer.
- **Docs:** 13 reference-doc rewrites.

The two foundational decisions are captured in [`_strategy/2026-06-control-plane.md`](./_strategy/2026-06-control-plane.md) and [`_strategy/2026-06-ai-harness.md`](./_strategy/2026-06-ai-harness.md); the now-historical pre-control-plane audit is in [`spec-audit.md`](./spec-audit.md).

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
