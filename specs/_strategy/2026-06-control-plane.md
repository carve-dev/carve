# 2026-06 — Carve is a control plane, not a project

> **Status:** Decided 2026-06-16 (Nate). Foundational architecture decision. Refines [`2026-05-positioning.md`](./2026-05-positioning.md) (OSS agent over dlt + dbt; hosting + UI is the paid product) with the structural model. Many capability specs are currently "project-shaped" and will need revision against this — see *Impact* below.

## The decision

Carve is a **control plane** that **references** independently-versioned, independently-deployed **code components** (an Extract-Load/dlt component, a Transform/dbt component, plus `sql`/other steps) — it does **not** contain them. The control plane holds the orchestration entities (pipelines, steps, schedules, jobs, runs, deploys); the components hold the code and each follow their own repo / CI-CD / lifecycle. This is option **C** from the SDLC discussion: *control-plane-shaped architecture, with single-repo "simple mode" as a first-class default.*

This replaces the implicit prior model where a "Carve project" was one repo/SDLC that fused the control plane (`pipelines/`) with the EL component (`el/`), and treated dbt as the *only* separable component. That asymmetry was the root cause of recurring design muddiness (the separate-repo deploy gap, the `carve el deploy` vestige, the two-"deploy" confusion).

## Canonical value proposition

**Tagline:** *Build, schedule, and monitor pipelines — all with AI.*

**Full statement:** Carve is a control plane that **schedules, orchestrates, and monitors** `dlt` + `dbt` + `sql` pipelines. Its AI can **build and deploy** those components for you, **or** you bring your own (built outside Carve) and Carve just orchestrates them.

- **Orchestration (schedule + run + monitor) is the core** — the durable substrate, always present regardless of how the code was built.
- **AI-native authoring + the cross-cutting build → deploy → schedule → monitor loop is the differentiator** — the answer to "why not Dagster + an agent?" and "why not dltHub Pro?". Framing orchestration *alone* as the value collapses Carve into "a simpler Dagster," which the positioning explicitly rejects.
- **Two modes, both first-class:** build-with-Carve (AI authors components into their repos) and orchestration-only (Carve references existing dlt/dbt/sql by version and only composes/schedules/monitors — PRD §6.2 mode 2). This duality is exactly *why* the control plane references components rather than owning them.

**Timing:** dlt **and dbt authoring are co-equal** — both authored from the start (the dbt-authoring deferral was reversed 2026-06-18); `sql` steps are user-authored at first. Orchestration-only mode is present from the start across all three (run dbt without authoring it).

## Why

- **Every comparable agrees.** Dagster (code locations), Airflow 3 (DAG bundles pinned to a git commit, multiple per deployment), Prefect (deployments via `from_source`, version = git hash), dbt Cloud (one repo per project, ingestion *excluded*), Fivetran/Airbyte (no ingestion repo at all) — all are **multi-component: a central orchestrator references independently-deployed code.** "One repo / one deploy for ingestion + transform + orchestration" exists only as a small-team convenience teams outgrow "for stronger isolation."
- **It matches the real adoption path.** The brownfield org with an existing dbt repo (its own CI/CD, its own team) is the bigger prize and is structurally multi-component. A single CI/CD literally cannot span "no-repo SaaS connector" and "a versioned dbt git repo."
- **It dissolves the asymmetry.** Once EL is a component like dbt (not fused to the control plane), deploy, topology, and the binding contract all become symmetric and the muddiness goes away.

## The model

- **Control plane** — the thing you run (`carve serve`), backed by Postgres. Contains pipelines, steps, schedules, jobs, runs, deploys, and **versioned references** to the code components. It is an *instance/deployment*, **not** a monorepo of your code. *(What we call it — "control plane" / "workspace" / "deployment" — is an open sub-decision.)*
- **Components** — independently-versioned, independently-deployed code the control plane references: EL (dlt), Transform (dbt), and `sql`/other steps. Carve's AI authors *into* a component; the component deploys on its own track (its own repo / CI-CD / lifecycle). EL and dbt are **symmetric** — no privileged, fused `el/`.
- **Binding contract** — the pipeline composition (today `pipelines/<name>.toml`) is Carve's analog of a Dagster code-location reference / Airflow DAG bundle: it references each component **by a pinned version** (commit/tag/ref — *not* branch-HEAD, which is the current gap), composes them into a step DAG (dlt → dbt → sql), and attaches a schedule. Cross-component ordering (ingest-before-transform) falls out of the DAG.
- **Simple mode (the delightful default)** — a small team can keep the control-plane config and all components in one repo (single working tree). This is the greenfield wedge and where AI is a huge boost. It does **not** change the architecture — the control plane still *references* components; they just happen to be co-located.
- **Why not Dagster** — Carve stays radically simpler by **scope and opinion**, not by being less capable: only `dlt` + `dbt` + `sql` step types, opinionated conventions, AI-driven authoring, no general asset framework, no "adopt our runtime's worldview." Dagster makes you adopt their way; Carve makes it simple. **Positioning line: "Build, schedule, and monitor pipelines — all with AI."**

## The shift, concretely

`carve.toml` stops being "the project root that contains `el/` + `pipelines/` + your code" and becomes **the control-plane instance config**: which components it references (pinned), its pipelines, its schedules, its runtime tuning. `el/` is no longer privileged — it is one referenced component, symmetric with dbt. The existing **orchestration-only mode** (PRD §6.2 mode 2 — Carve writes a `pipelines/*.toml` referencing the user's existing dlt+dbt by path, no code-gen) is the seed of the correct model and becomes central rather than a corner case.

## Resolved design decisions (2026-06-16)

These refine the model above; concrete config shapes are in [`control-plane-reference-model.md`](./control-plane-reference-model.md).

- **Nouns & shape.** No container noun — the first-class nouns are **pipelines, components, schedules**; `carve.toml` is the Carve (control-plane) config. ("instance" only if a word is ever forced; avoid "workspace"/"deployment"/"project" — all collide or mislead.) Control-plane definitions are **config-as-code reconciled into the state store**: the state store is a materialized projection of the version-controlled definitions, refreshed on `carve serve` boot + a periodic loop.
- **Three-tier code/data ownership.** Reconciliation is per-concern, not uniform:
  - *Pipeline definition* (steps, DAG, component refs, pins) → **code** (`pipelines/<name>.toml`), reconciled into state; code wins.
  - *Schedule* (cron, cadence, enabled/paused) → **data** (`schedules` table), set via CLI/API/UI — instant + audited. Code may carry an optional `[seed_schedule]` block applied only at first registration; thereafter the live schedule is data and editing `[seed_schedule]` is a no-op unless `--reseed`.
  - *State* (jobs, runs, history) → **data**, always.
  - **Reverses UC2's resolved decision** that schedule changes go through plan/build/deploy/PR. Audit now comes from the `schedule_changes` log + the `schedule` scope (RBAC), not git. This **deletes UC2's code-vs-override TTL-precedence machinery** — the reconciler never touches the schedule. Tradeoff: schedules reconstitute from the (backed-up) state store + the code seed, not from `git clone`.
- **Symmetric components.** EL (dlt) gets the same topology model as dbt (`same-repo` / `separate-local` / `separate-remote`); no privileged fused `el/`.
- **Version pinning.** The binding contract supports a per-component **pinned ref (commit/tag)** from day one (the primitive that makes multi-component reproducible + coordinated), but **defaults to branch-HEAD** in simple/single-repo mode (zero friction). Full lockfile + auto-bump-bot deferred.
- **Progressive disclosure.** Simple mode is convention-driven — `el/` is the dlt component, the detected dbt project is the dbt component; no `[components.*]` blocks, no pins. The reference/pin apparatus **materializes only when a component is split out**, and is always inspectable (`carve components show`).
- **Graduation (simple → multi).** Enabled by **name-based indirection** — pipelines reference a component by name; resolution (local path vs remote repo @ ref) is a separate per-component setting. Graduating a component = extract its code to a new repo + one guided command (`carve component <name> --separate-remote <url> [--ref <pin>]`) that writes the block, clones to the workspace cache, validates. **No pipeline rewrites, no state migration, no re-runs; incremental (per-component), reversible, symmetric with brownfield** ("born multi" via `carve init --dbt-url` is the same machinery). Graduation is what makes the cross-repo **linked-PR deploy** worth building.

## Still-open sub-decisions

4. **Deploy under the control-plane model — RESOLVED (2026-06-16): linked-PR ships in the first usable cut.** `carve deploy` promotes a component (its repo) + the control-plane composition, and cross-repo **linked-PR coordination** (ingest-first ordering) is **built up front**, not deferred to a later increment — spec 14's separate-remote deferral is reversed. `carve el deploy` retires or shrinks to a thin target-readiness `verify`. The delivery plan sequences this work item (spec 14 + ARCHITECTURE §7.5/§9.4/§10).
5. **Initial scope/bundling.** Control plane + EL component + simple-mode is the initial wedge; multi-component separation is the bigger appeal it must not preclude. *Open: does the shipping order / pillar bundling change under control-plane framing?*
6. **Pillar restructure.** Re-articulate the four pillars around control-plane vs components (the README rewrite).

## Impact (docs/specs currently "project-shaped" — to revise)

- `specs/README.md` (stale pillar table *and* needs control-plane reframe)
- `PROJECT_PLAN.md` (pillar bundling + "project" language)
- `ARCHITECTURE.md` (§7.5 deploy, §9.4 deploys, §10 topology; add control-plane / component / versioned-reference concepts)
- `PRD.md` (§6.2 separate-repo, §6.8 deploy, "project" framing; mode 2 = the seed) — **adopt the canonical value proposition (above) in the PRD opening** during the reposition pass
- `specs/capabilities/layout.md` (add `[el]` topology symmetric with dbt; `carve.toml` → control-plane config)
- `specs/capabilities/dlt-engineer.md` (authors into a component, not fused `el/`)
- `specs/capabilities/pipelines.md` (binding contract gains versioned references)
- `specs/capabilities/deploy.md` (control-plane-aware; cross-repo coordination first-class; reconcile `el deploy`)
- `specs/capabilities/packaging.md`, `05-init-rewrite.md`, `13-reference-docs.md` (init scaffolds a control plane + optional simple-mode; config schema)
- `specs/use-cases.md` (cross-cutting model; brownfield/separate-repo walkthroughs become central)
