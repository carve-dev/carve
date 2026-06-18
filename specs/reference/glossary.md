# Reference — Glossary

Terms used throughout the Carve documentation, codebase, and UI. Where a term has a specific meaning in Carve different from its general meaning, the Carve definition takes precedence. Alphabetical.

---

**Agent**
A declarative role definition the AI harness runs: a markdown file with YAML frontmatter (`name`, `description`, `model`, `tools`, `allowed_paths`, `max_mode`, `classifications`) plus a system-prompt body. Built-ins live at `src/carve/core/agents/builtin/<name>.md`; a user file at `carve/agents/<name>.md` overrides a built-in of the same name. See [extensibility](../capabilities/extensibility.md).

**Agent loop**
The conversational pattern where an agent receives a goal, calls tools, reads results, and continues until it produces an answer or hits a limit. The harness adds permission gating, context management, and subagent delegation on top of the base loop. See [harness](../capabilities/harness.md).

**Ask**
A read-only investigative query, answered by the **explorer** subagent running in `read_only` mode. Produces a cited answer and changes nothing. Invoked via `carve ask`. See [ask](../capabilities/ask.md).

**Backend**
In Carve terminology, "backend" means dlt or dbt (the external tools Carve drives) — *not* a database or a server-side application.

**Brownfield**
Adopting Carve into an existing dbt/dlt project. Carve infers conventions and references the existing components without rewriting them. The orchestration-only / brownfield path is a central adoption mode, not a corner case. Contrast with greenfield.

**Build**
The verb that materializes a reviewed Plan into files (dlt code, `pipelines/<name>.toml`). `carve build <plan_id>` writes files and emits the `post_build` hook; it does not deploy. Distinct from plan (no files) and deploy (promotion).

**Component**
An independently-versioned dlt or dbt unit the control plane references **by name** (`component = "<name>"`), rather than containing. In simple mode components are discovered by convention (each `el/<name>/` is a dlt component; the detected dbt project is a dbt component); in multi mode they are declared as `[components.<name>]` blocks in `carve.toml`. See [layout](../capabilities/layout.md).

**Component locator**
The resolver that turns a component name into a concrete code location — a local path (simple / separate-local mode) or a remote repo at a pinned ref (separate-remote mode) cloned into the workspace cache.

**Control plane**
Carve's core identity: it builds, schedules, and monitors pipelines by *referencing* independently-versioned dlt/dbt/sql components, rather than being a project that contains them. `carve.toml` is the control-plane config. See [`_strategy/2026-06-control-plane.md`](../_strategy/2026-06-control-plane.md).

**Conventions**
A project's inferred house style (naming, layout, tagging, test patterns) in `carve/conventions.md`. Carve-generated and refreshable; loaded into agent context so generated code matches existing code. See [memory](../capabilities/memory.md).

**Convention inference**
The brownfield process where Carve scans an existing dbt/dlt project and writes `conventions.md` reflecting current practice. Re-runnable via `carve memory refresh`.

**DAG**
Directed acyclic graph. Used for pipeline steps (a step's `depends_on`) and for dbt model dependencies (refs).

**dbt**
[Data build tool](https://docs.getdbt.com/) — the SQL transformation framework Carve drives for the transform phase. Carve treats dbt-core as a runtime dependency. AI authoring of models is provided by the dbt **engineer**; Carve also runs dbt via the `dbt` step type.

**dbt manifest**
dbt's compiled project representation (`target/manifest.json`). Carve reads it (via the `dbt_manifest` skill) for model dependencies, sources, and tests — this *is* dbt's model-level lineage. See [lineage](../capabilities/lineage.md).

**DCO**
Developer Certificate of Origin — a sign-off (`git commit -s`) certifying the contributor's right to submit. Required on every commit; chosen over a CLA to lower friction. See [governance.md](./governance.md).

**Delegation**
The orchestrator handing a scoped task to a subagent via a synchronous `delegate` call that returns a `DelegationResult`. The child's permission mode is clamped to `min(parent, agent)`. The mechanism behind the engineer + review-subagent pattern. See [harness](../capabilities/harness.md).

**Deploy**
Promoting built code to a target via a **configurable handoff** (`files` | `commit` | `push` | `pr`; default `pr`). Cross-repo graduated components produce coordinated linked PRs. `carve deploy <pipeline>`. See [deploy](../capabilities/deploy.md).

**dlt**
[Data load tool](https://dlthub.com) — the Python library Carve generates and runs for the extract-load phase. Carve authors dlt code; dlt executes it and maintains its own schema. See [dlt-engineer](../capabilities/dlt-engineer.md).

**dlt resource**
A dlt construct: one endpoint or table inside a source (e.g., `charges`). dlt's stored schema records which resource produced which destination table — read via the `dlt_schema` skill.

**dlt source**
A dlt construct: a logical connector (e.g., Stripe) containing one or more resources.

**Embedding search**
Semantic search over indexed model descriptions / column docs via vector embeddings, for fuzzy concept lookup. An in-scope retrieval layer alongside catalog + manifest + grep + investigation.

**Event bus**
The internal pub/sub for runtime events (`job.*`, `run.*`, `step.*`, `schedule.*`). In-process for OSS; the seam where hooks and webhooks subscribe. See [runtime](../capabilities/runtime.md).

**Explorer**
The read-only subagent behind `carve ask` — investigates code, dbt manifest, dlt schema, and live `INFORMATION_SCHEMA` to answer how/where/why/lineage questions, citing what it found. Runs in `read_only` mode. See [ask](../capabilities/ask.md).

**Graduation**
Moving a component from simple mode (convention-discovered, in-repo) to multi mode (its own local path or remote repo), via `carve component <name> --separate-local/--separate-remote`. Reversible with `--same-repo`. See [pipelines](../capabilities/pipelines.md).

**Greenfield**
Starting a new project with no existing dbt/dlt. `carve init` scaffolds the structure. Contrast with brownfield.

**Harness (AI harness)**
Carve's Claude-Code-style agentic engine: an agentic loop + terminal-grade tools (edit/bash/grep/web) + a permission system + context management (subagent isolation + compaction), specialized for data work. The cross-cutting engine all the agents run on. See [`_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md).

**Hook**
A user-defined command run at a tool or lifecycle seam (`pre_tool`, `post_tool`, `pre_deploy`, `post_build`, `on_run_failed`), declared in `carve/hooks.toml`. Runs through the same `bash` gate, mode-clamped, fail-closed. See [extensibility](../capabilities/extensibility.md).

**Hosted product**
Carve's commercial offering: multi-tenant, managed infrastructure, SSO/OAuth/RBAC, audit log, a polished cloud UI, premium integrations, hosted secrets. Earns its price on operational excellence, not feature gating — no API/MCP surface is withheld from OSS. See [governance.md](./governance.md).

**Idempotency**
The property that re-running an operation yields the same result. The job queue enforces at most one queued + one running job per pipeline (partial unique indexes); the reconciler reconstitutes definitions without duplication.

**Investigation**
A recovery diagnosis: a Postgres row capturing a failure's classification, markdown diagnosis, proposed Plan, and resolution status. Produced by the recovery engineer on retries-exhausted failures; surfaced via `carve investigations`. See [recovery](../capabilities/recovery.md).

**Job (runtime)**
A row in the `jobs` table representing one queued or executing pipeline invocation. Distinct from a Run (which records the execution). See [runtime](../capabilities/runtime.md).

**Lineage**
The relationship between data assets. dbt provides model-level lineage (its manifest) and dlt maps each resource to the destination table it writes (its stored schema). Carve maintains **no** lineage store of its own — the explorer **investigates** these native sources (plus the code) on demand to answer "where does this come from / what breaks if I change this" ([lineage](../capabilities/lineage.md)). Pipeline-level lineage ("which step refreshes which table") falls out of dlt's schema + the pipeline definitions (component-by-name).

**MCP**
[Model Context Protocol](https://modelcontextprotocol.io/) — the standard for connecting LLMs to external tools. Carve consumes external MCP servers as namespaced, effects-tagged skills (`mcp:server:tool`) and exposes itself as an MCP server (`carve mcp-serve`). See [mcp-server](../capabilities/mcp-server.md), [extensibility](../capabilities/extensibility.md).

**Multi mode**
The configuration where components live in their own paths or repos, declared as `[components.<name>]` blocks in `carve.toml` (typically pinned). Reached by graduation from simple mode. Contrast with simple mode.

**Optimistic claim**
The job-queue pattern: `UPDATE ... WHERE status='queued' ... FOR UPDATE SKIP LOCKED`. Lets multiple workers pull from one queue without a broker. See [runtime](../capabilities/runtime.md).

**Orchestrator**
The harness's main loop: classifies a goal, gathers bounded context, and delegates scoped tasks to subagents (engineers, reviewers, the explorer, recovery). Owns refinement and the review fan-out; does not author component code itself. See [harness](../capabilities/harness.md).

**OSS edition**
The open-source Carve (this repo), Apache 2.0, feature-complete for single-team self-hosters. Anything that ships in the initial release stays OSS. See [governance.md](./governance.md).

**Permission mode**
One of `read_only` | `plan` | `build` | `deploy` — the harness's escalating capability tiers. The **permission gate** enforces them (and `allowed_paths`, the bash sandbox, secret-path denial) at the tool-call boundary; agent grants are *attenuated* to `grant ∩ mode-permitted`, never widened. Fail-closed. See [harness](../capabilities/harness.md).

**Pin**
A fixed component revision (commit SHA or tag) recorded as `ref` in a `[components.<name>]` block; the locator checks out exactly that revision. `ref` wins over `branch`; absent both, the remote's default-branch HEAD is tracked. Simple-mode components are never pinned.

**Pipeline**
A named DAG of steps (`dlt` / `dbt` / `sql`), defined in `pipelines/<name>.toml`. The unit users think about ("run my daily revenue pipeline"). Composed from components by name. See [pipelines](../capabilities/pipelines.md).

**Pipeline engineer**
The subagent that composes components by name into `pipelines/<name>.toml`, verifying via `carve pipelines validate` + a dev run. The control-plane runtime specialist. See [pipelines](../capabilities/pipelines.md).

**Plan**
A persisted, reviewable representation of intended changes (goal, summary, expected effects), produced by `carve plan`. Files are written only on `carve build`. Lives under `.carve/plans/`. Refinable; refinements chain via `parent_plan_id`.

**Provenance header**
The comment block atop Carve-generated dlt code recording what generated it, from which source, at what commit, and which plan/build. Carve regenerates below the header and preserves user edits; a file without the header is treated as user-authored and never modified. See [layout](../capabilities/layout.md).

**Reaper**
The runtime loop that reclaims jobs from crashed workers via stale-heartbeat detection. See [runtime](../capabilities/runtime.md).

**Reconciler**
The loop in `carve serve` that reconciles each `pipelines/<name>.toml` *definition* (steps, DAG, component refs, pins) into state — code wins. It seeds a schedule row from `[seed_schedule]` at first registration but never afterward touches the live schedule (which is data). See [pipelines](../capabilities/pipelines.md).

**Recovery engineer**
The subagent that diagnoses a retries-exhausted failure (grounded in dlt exception classes, schema diff, run logs), then **delegates the fix** to the DLT, dbt, or SQL engineer. Never writes component code or auto-deploys; produces an Investigation + a reviewable Plan. See [recovery](../capabilities/recovery.md).

**Refine**
Iterating on a Plan with feedback (`carve plan --refine <plan_id> "<feedback>"`), producing a child plan in the same chain.

**Repo topology**
Same-repo vs separate-local vs separate-remote placement of a component's code — chosen per component, independently. See [layout](../capabilities/layout.md).

**Run**
A single execution of a pipeline: a row with status/timing/cost and per-step subrecords, plus streamed logs. Persisted in Postgres (with an active→archive lifecycle). Distinct from a Job (the queue entry).

**Runtime**
Carve's deliberately-narrow execution layer: scheduler + Postgres-backed job queue + worker pool + reaper + archiver. No asset-graph reactivity, conditional branching, or cross-pipeline triggers (those are explicitly out of scope). See [runtime](../capabilities/runtime.md).

**Schema retrieval**
The pattern of letting agents query the schema (catalog, dbt manifest, dlt schema) on demand rather than memorizing it in the prompt. Implemented via reader skills (`dbt_manifest`, `dlt_schema`, catalog introspection); lineage is investigated, not stored (see *Lineage*).

**Seed schedule**
The optional `[seed_schedule]` block (`cron` / `timezone` / `target`) in `pipelines/<name>.toml`. A one-time **seed** of the live `schedules` row at first registration — not the source of truth, and it cannot pause (no `paused`/`enabled` key). Re-applied only via `carve schedule reseed`. The live schedule is data, changed via `carve schedule`. See [runtime](../capabilities/runtime.md), [pipelines](../capabilities/pipelines.md).

**Simple mode**
The default single-repo configuration: no `[components.*]` blocks, components discovered by convention, schedules from `[seed_schedule]`, branch-HEAD (unpinned). The delightful zero-friction default; teams graduate to multi mode incrementally. Contrast with multi mode.

**Skill**
A capability an agent can use. Built-in callable skills are `@skill` functions (catalog introspection, `dbt_manifest`, `dlt_schema`, `memory_read`); external MCP tools arrive as namespaced skills. Distinct from a skill pack (content, not a callable). See [extensibility](../capabilities/extensibility.md).

**Skill pack**
A folder (`carve/skills/<name>/SKILL.md` + optional `scripts/`/`resources/`) that surfaces as description-matched **content injected into context** — not a callable tool. The curated connector library ships as skill packs. See [extensibility](../capabilities/extensibility.md).

**Source of truth**
The authoritative location of state. Pipeline *definitions* are code (git); the *schedule* and *run state* are data (Postgres); dbt/dlt own their respective schemas. The three-tier code/data split underpins the control-plane model.

**SQL tool**
The dialect-aware capability every agent uses (parse/validate/transpile via `sqlglot`, per-dialect `INFORMATION_SCHEMA` introspection, role-gated execution). SQL is a cross-cutting *tool*, not an agent. A thin SQL specialist handles explicit authoring. See [sql](../capabilities/sql.md).

**State store**
The Postgres database that persists pipelines, plans, builds, jobs, runs, steps, logs, schedules, investigations, and audit trails. SQLite was retired in spec 01. See [state-store](../capabilities/state-store.md).

**Static HTML UI**
Carve's minimal local web UI: pages (run history, per-run detail + logs, pipelines) regenerated on run events and served on loopback by `carve docs serve`. No live updates, no auth, no lineage view. The polished operational UI is the hosted product. See [ui](../capabilities/ui.md).

**Step**
The unit of execution inside a pipeline. There are three step types — `dlt`, `dbt`, `sql` — each with `id`, `depends_on`, and a `[steps.failure_mode]` (`fail`/`warn`/`continue`/`retry`/`skip_downstream`). `dlt`/`dbt` steps reference a component by name; `sql` steps reference a file + connection. See [pipelines](../capabilities/pipelines.md).

**Subagent**
A specialist agent the orchestrator delegates a scoped task to, running in its own isolated context and returning a summary (the context-isolation mechanism that keeps the main loop bounded). The DLT engineer, pipeline engineer, recovery engineer, explorer, and the review subagents (dlt-qa, dlt-security) are subagents. See [harness](../capabilities/harness.md).

**Target**
A named environment (e.g., `dev`, `prod`) defined in `carve/connections.toml`, carrying a dialect, credentials, and role scoping. `default_target` is set in `carve.toml`.

**Token (API)**
The bearer token authenticating REST/MCP requests. The OSS install writes a single token to `.carve/token` (mode 0600); `carve auth token rotate` mints a new one. Hosted adds scoped service accounts. See [rest-api](../capabilities/rest-api.md).

**TOML**
The config format for `carve.toml`, `pipelines/<name>.toml`, `connections.toml`, `runtime.toml`, `hooks.toml`, and `mcp.toml`. (Agents and skill packs are markdown, not TOML.)

**Verify-by-execution**
The harness's accuracy primitive: an engineer generates code, *runs* it (e.g., `dlt pipeline run`, `dbt build`, `carve pipelines validate`), reads the parsed result, and self-corrects within bounded iterations before returning a Plan. Grounding in real tool output over the model's guesses. See [harness](../capabilities/harness.md).

**Workspace cache**
The local cache (`.carve/workspaces/<derived-name>/`) where separate-remote components are cloned at their pinned ref for use. Managed by Carve; cleared via `carve workspaces clear`. See [layout](../capabilities/layout.md).
