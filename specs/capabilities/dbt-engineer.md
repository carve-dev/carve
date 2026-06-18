# dbt engineer: authoring dbt models, tests, and sources

> The **dbt authoring subagent** — the exact parallel to the [DLT engineer](./dlt-engineer.md), one tier up the stack. A declarative agent on the [harness](./harness.md) that **writes and modifies** dbt models, tests, snapshots, and `sources.yml` entries to fit the project's conventions, and **verifies by executing** (`dbt build`/`test`) until green — with a **dbt-qa** review subagent for coverage/convention. It is **backend-agnostic**: it authors the dbt *code*; [`dbt-execution`](./dbt-execution.md) runs it however the component runs (bundled / external / dbt Cloud / snowflake-native). *Phasing annotation:* authoring follows dlt authoring — Carve **runs** dbt from the start (via `dbt-execution`), and **authors** it in a later increment; the increment is a [DELIVERY](../DELIVERY.md) decision, not set here.

## Status

- **Status:** Drafting
- **Depends on:** [harness](./harness.md) (subagent + delegation + permission gate + verify-by-execution), [extensibility](./extensibility.md) (the declarative agent + the `dbt_manifest` skill it leans on), [sql](./sql.md) (dialect-aware authoring/validation of model SQL), [dbt-execution](./dbt-execution.md) (how it runs `dbt build`/`test` to verify), [memory](./memory.md) (conventions/standards it writes to), [layout](./layout.md) (the dbt component it authors into).
- **Used by:** [pipelines](./pipelines.md) (the authored models become `dbt` steps), [recovery](./recovery.md) (delegates a model-side fix here).
- **Lineage:** net-new. The Pillar-3 "dbt agent" long deferred to "v0.2" — now a first-class capability with phasing as an annotation, per [`_strategy/2026-06-spec-structure.md`](../_strategy/2026-06-spec-structure.md).

## Goal

Let the AI **author and modify** a dbt component the way the DLT engineer authors a dlt component: generate models/tests/sources that match the project's conventions, run them, read the result, and self-correct — returning a reviewable Plan. Plus a **dbt-qa** reviewer for test coverage + convention adherence. Backend-agnostic authoring; [`dbt-execution`](./dbt-execution.md) owns *running*.

## Out of scope

- **Running dbt** — execution (bundled/external/cloud/native, engine choice) is [`dbt-execution`](./dbt-execution.md). This capability only *authors*; it verifies *through* that one.
- **dbt the tool's internals** — the DAG, materializations, test framework, manifest are dbt's. Carve authors code that uses them well (same stance as dlt).
- **Orchestration-only shops** — a team that brings finished dbt and only wants scheduling/monitoring never invokes this agent (they use `dbt-execution` + [pipelines](./pipelines.md) + [lineage](./lineage.md)).

## Behavior

### The subagent (declarative, on the harness)

A built-in agent (`src/carve/core/agents/builtin/dbt-engineer.md`, the [extensibility](./extensibility.md) markdown+frontmatter format) the orchestrator `delegate`s dbt-authoring tasks to, under the `build` permission mode:

- **Tools:** `edit`/`create_file` scoped (`allowed_paths`) to the dbt project's `models/**`, `tests/**`, `snapshots/**`, `*_schema.yml`, `sources.yml`; `grep`/`glob`; the **dbt skills** (`list_models`, `model_columns`, `model_dependencies`, `tests_on_model` — the `dbt_manifest` family) + the dialect-aware [`sql`](./sql.md) tool on the read role.
- **Classifications:** `new_model`, `modify_model`, `add_tests`, `declare_source`, `refactor_models`.
- **Verify by execution:** authors → runs `dbt build`/`dbt test --select <models>` **through [`dbt-execution`](./dbt-execution.md)** against a dev target (whatever backend the component uses — a bundled-engine dev run, or a dbt Cloud / snowflake-native dev job) → reads the per-model result → fixes within bounded iterations + a cost ceiling, before returning a Plan.

### The dbt-qa reviewer

A review subagent (parallel to [dlt-qa/dlt-security](./dlt-engineer.md)) the orchestrator fans out after authoring: checks **test coverage** (are new models tested? freshness/uniqueness/not-null/relationships where apt), **convention adherence** (naming, layout, tags, materializations vs the project's inferred conventions in [memory](./memory.md)), and **SQL quality** (via [`sql`](./sql.md)). Adversarial, advisory; the orchestrator owns the fan-out.

### Brownfield convention inference + greenfield scaffolding

- **Brownfield:** infers the existing project's conventions (naming, layout, materialization, test patterns) into [memory](./memory.md) and authors *in that style* — never rewriting existing models unasked (same provenance discipline as dlt).
- **Greenfield:** scaffolds a dbt project (`dbt_project.yml`, a staging/marts layout, sample models + tests) when the user has no dbt yet.

### Cross-backend source coupling

The DLT engineer lands data into schemas the dbt engineer declares as `sources.yml` entries — so the dlt → dbt boundary is explicit and inspectable regardless of repo topology. When both agents act on one goal, the orchestrator sequences ingest-then-transform and keeps the source contract aligned.

## Tests

- **Integration (author + verify):** `carve plan "add a daily revenue mart on top of stg_orders"` delegates to the dbt engineer, which authors the model + tests, runs `dbt build --select` via `dbt-execution` on a dev target, and self-corrects a deliberately-broken ref before returning a Plan.
- **Integration (dbt-qa):** an authored model with no tests is flagged by dbt-qa; a model violating the inferred naming convention is flagged.
- **Unit (brownfield style):** authored SQL matches the inferred `stg_`/`mart_` conventions from a fixture project.
- **Unit (backend-agnostic):** the same authoring flow verifies through a stubbed local backend and a stubbed snowflake-native backend without change.

## Acceptance

- The dbt engineer authors/modifies models, tests, and sources matching project conventions, **verifying by executing** through whatever [`dbt-execution`](./dbt-execution.md) backend the component uses, and returns a **reviewable Plan** — never deploying autonomously.
- A **dbt-qa** reviewer surfaces coverage/convention gaps.
- Authoring is **backend-agnostic** — a team on dbt Cloud or snowflake-native gets the same authoring, verified via their backend.
- The capability is documented as durable design with phasing as an annotation (no increment hard-coded).

## Design notes

- **Why split authoring from execution?** A huge share of Carve's audience runs dbt (dbt Cloud, snowflake-native) but may never want Carve to *write* models — they want orchestration + monitoring. Execution must stand alone; authoring is additive. (Symmetric with how the DLT engineer both authors and runs dlt, but dbt's execution is richer — hence the split.)
- **Why verify through `dbt-execution`?** So authoring is correct against the team's *actual* runtime (their engine/adapter/backend), not a Carve-only assumption — a model that passes on bundled dbt-core but breaks on their Fusion/Snowflake setup isn't "verified."

## Open questions

- **Fusion authoring affordances.** Fusion's SQL comprehension can validate model SQL *before* a warehouse run; the dbt engineer should use that as a cheaper inner-loop check when the backend is Fusion. Confirm the interface when `dbt-execution`'s Fusion path lands.
- **Phasing.** The increment that introduces authoring is a [DELIVERY](../DELIVERY.md) call — likely after the dlt-engineer + dbt-execution land.
