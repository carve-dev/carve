# DLT engineer subagent: authors and runs dlt code

> **Revised for the AI-harness model** (see [../_strategy/2026-06-ai-harness.md](../_strategy/2026-06-ai-harness.md)): the EL specialist becomes the **DLT engineer subagent** — a *declarative* agent (built-in at `src/carve/core/agents/builtin/dlt-engineer.md`, the spec-16 frontmatter format) that the orchestrator `delegate`s to (spec 15), armed with **terminal-grade tools** (`edit`/`bash`/`grep`/`web_fetch`) + dlt skills + the `sql` tool (spec 18), running in **`build`** permission mode, that **closes the loop** by executing the component through Carve's venv runner and self-correcting on the parsed load-package result (the verification primitive, spec 15). Its diff then passes through **dlt-qa** and **dlt-security** review subagents (the `/build-spec` engineer→reviewers→fix pattern, brought to users' pipelines). The hardcoded `extract_load` agent class is retired in favor of this declarative agent on the harness.

> **Still revised for the control-plane model** (see [../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md)): the DLT engineer authors *into a named `dlt` component* — its `el/<name>/` directory in simple mode, or its own repo in separate mode — not a privileged fused `el/`. Dependency hints are emitted by component name. The four authoring strategies, provenance header, skills, the component-authoring model, and CDC scope note are all preserved; this revision layers the harness model on top of them.

> The wedge. The DLT engineer takes a goal slice from the orchestrator (via `delegate`) and produces working, **verified** dlt code in the target `dlt` component (the `el/<component_name>/` directory in simple mode), picking among four authoring strategies (native dlt source, REST API generic config, curated library copy, Singer/Airbyte wrapper) and then running it to green. Per [PRD §5.2](../PRD.md), [PRD §6.3 project memory](../PRD.md), [ARCHITECTURE §2.2 agent layer](../ARCHITECTURE.md), [ARCHITECTURE §5.1–5.4](../ARCHITECTURE.md), [ARCHITECTURE §5.8 curated library](../ARCHITECTURE.md), and [ARCHITECTURE §10.2 dlt invocation](../ARCHITECTURE.md). Runs on the harness ([harness](./harness.md)) as a declarative agent ([extensibility](./extensibility.md)). Replaces the archived [P1-04 extract-load agent](../_archive/pillar-1-extract-load/04-extract-load-agent.md), whose premise (agent authors bespoke Python with `executemany`/`MERGE`) was broken by the dlt-backend positioning.

## Status

- **Status:** Drafting
- **Depends on:** [state-store](./state-store.md), [layout](./layout.md), [harness](./harness.md) (subagent delegation, terminal tools, permission modes, the verification loop), [extensibility](./extensibility.md) (the declarative agent format this agent ships in), [sql](./sql.md) (the `sql` tool the agent uses for schema checks).
- **Blocks:** [init](./init.md), [runtime](./runtime.md) (the `dlt` step type executes what this agent produces), [pipelines](./pipelines.md), [recovery](./recovery.md) (recovery `delegate`s dlt fixes to this agent).
- **Built on:** the orchestration agent and reasoning loop from M1 (HISTORICAL — preserved, not rewritten, and evolved into the harness orchestrator/main loop per spec 15). This spec defines the DLT engineer as a declarative subagent on that harness.

## Goal

Ship the **DLT engineer** as a declarative subagent on the harness, armed to author *and verify* dlt code. Concretely:

- Is `delegate`d a task by the orchestrator (spec 15): a goal slice + a context bundle (the target `dlt` component name, destination, existing sources, memory files, optional curated-library match, optional brownfield component references). It runs in its own isolated context and returns a **summary**, not its transcript.
- Picks one of four authoring strategies based on the goal and context
- Authors dlt files into the target component using the `edit` tool (read-before-edit, string-replace) — in simple mode its `el/<component_name>/` directory, resolved by name per [layout](./layout.md) — plus the relevant `.dlt/config.toml.template` / `.dlt/secrets.toml.template` entries. Writes are confined to `allowed_paths` by the permission gate (spec 15).
- Records provenance headers per [layout](./layout.md)
> **Updated during implementation (2026-06-25):** authoring + verify is a **build-time** behavior. When the orchestrator delegates this engineer **for a `carve plan`** (the `capacity == "design"` context flag, [plan-build](./plan-build.md) §"Plan synthesis"), it runs at **plan/read authority** — it uses its read tools + domain expertise to *propose* a strategy + file manifest + cost/runtime estimate, authoring and verifying **nothing** (`carve plan` is the human-in-the-loop gate; code is authored only at `carve build` after the human accepts). The bullets below (author, close the loop, verify) describe its **build** capacity.

- **Closes the loop:** executes the authored component through Carve's venv runner (the same primitive the runtime uses — `dlt` ships no `run`/`check` CLI subcommand; a dlt pipeline is a Python entrypoint, and freeform `python` is gate-denied, so execution is the structured runner, not raw `bash`), reads the harness-parsed `CheckResult` (spec 15's verification primitive, which parses the load package's `state.json`), and **self-corrects** until green — using the `sql` tool (spec 18) to confirm the real destination schema rather than guessing it. (`dlt pipeline <name> info`/`trace` via `bash` is available for inspection.)
- Hands its diff to the **dlt-qa** and **dlt-security** review subagents (below); the **orchestrator** routes the diff through them (sequentially) and feeds findings back to the engineer before the change is surfaced
- Returns a structured summary to the orchestrator: files written, their hashes, the verification result, expected outputs, dependencies on dbt sources (keyed by component name)
- Operates idempotently on modifications: re-running against an existing component diffs cleanly, preserves user edits below the provenance header

This spec ships the **declarative agent definition** (the built-in markdown agent + its system prompt body), the **review subagents**, the skills the agent calls, and the initial curated source library structure. The harness mechanics (delegation, `edit`/`bash`/`grep`/`web_fetch`, permission modes, the verification loop) come from spec 15; the declarative agent/skill format from spec 16; the `sql` tool from spec 18. The first wave of curated sources (Stripe, Salesforce, etc.) is a separate later effort; this spec ships the framework (now a **skill library**, spec 16) that those slot into.

## Out of scope

- **The harness mechanics** — subagent delegation (the `delegate` tool), the terminal tools (`edit`/`bash`/`grep`/`web_fetch`/`web_search`), permission modes/allowlists/sandbox, and the verification-loop primitive (`run_check`) — are [harness](./harness.md). This spec *consumes* them; it does not define them.
- **The declarative agent/skill format + registry** (frontmatter schema, hot-reload, name override, skill packs, the connector→skill library loader) — [extensibility](./extensibility.md). This spec ships *a* built-in agent file in that format and *a* set of skill packs; it does not define the format.
- **The `sql` tool layer** (dialect-aware introspection/validation/run, role-scoping) — [sql](./sql.md). The DLT engineer *uses* the `sql` tool for schema checks; it doesn't define it.
- The orchestrator's classification + delegation logic — that's the harness main loop (spec 15) matching a goal's classification against each agent's `classifications` (spec 16). This spec produces a DLT engineer the orchestrator `delegate`s to; it doesn't change the orchestrator.
- The dbt engineer subagent — that's a separate capability ([dbt-engineer](./dbt-engineer.md)).
- The pipeline composition step (`pipelines/<name>.toml`, whose steps reference components by name via `component = "<name>"`) — written by the **pipeline engineer** (the runtime specialist, [pipelines](./pipelines.md)). The DLT engineer emits a structured "this pipeline needs to be composed with these dependencies" hint, keyed by the component name it authored into; the orchestrator then `delegate`s the composition to the pipeline engineer.
- The actual contents of the curated source library beyond a small reference example (e.g., a "Hacker News API" sample). Curating the top-30 Airbyte ports is a separate, later workstream.
- The CLI/REST/MCP surface for invoking the agent directly. The `carve agents list/show/create/test` surface lives in [extensibility](./extensibility.md); the REST/MCP surface in [rest-api](./rest-api.md) and [mcp-server](./mcp-server.md).
- Orchestration-only mode (PRD §6.2 mode 2). When the user has an existing `dlt` component (in this repo or a separate-remote one), the orchestrator does NOT `delegate` to the DLT engineer for that pipeline — it `delegate`s to the pipeline engineer to compose the existing component by name. This spec handles modes 1 (authoring) and 3 (mix); mode 2 is a no-op for this agent.

## Behavior

### Agent definition (`src/carve/core/agents/builtin/dlt-engineer.md`)

> **Retirement landed (2026-06-26):** the hardcoded M1 `extract_load` agent is now **deleted** — `core/agents/extract_load/`, its prompt, `tools/extract_load_tools.py`, and the two orphaned skills `core/skills/{data_engineering,snowflake_destination}.md` are gone; this declarative agent is the sole el-authoring path. The allow-listed write tool the **recovery** agent uses was rehomed verbatim out of the deleted EL tools module into `m1_tools.py` as `make_allowlisted_write_file_tool` (recovery + `tool_binding.py` repoint to `m1_tools`); its path-containment is unchanged. The `el/<name>/` directory convention, the `carve el` CLI, and recovery's `ElRunInvocation`/`EL_RUN_FAILURE` el-run path are **kept**.

The DLT engineer is a **declarative agent** in the spec-16 format: frontmatter + a system-prompt body. It ships as a built-in (under `src/carve/core/agents/builtin/`); a user can override it by dropping `carve/agents/dlt-engineer.md` (spec 16's name-override). The hardcoded `extract_load/` agent package is retired — the harness loads this markdown file and runs it as a subagent.

> **Updated during implementation (2026-06-23):** the shipped frontmatter (a) **omits `model:`** so the agent falls back to the install `default_model` (spec 16's per-agent tiering is opt-in; the engineer doesn't pin a tier), (b) carries an explicit **`max_mode: build`** key (the advisory-lint ceiling, spec 16) rather than relying on prose, and (c) adds **`create_file`** to the grant alongside `edit` (net-new files — `__init__.py`/`requirements.txt` — vs. read-before-edit string-replace). The grant is written as a flat inline list. `lookup_skill_pack` is referenced by the body but is **intentionally not** in the grant — the orchestrator appends it at delegation time (see Open questions).

```markdown
---
name: dlt-engineer
description: >
  Authors and runs dlt sources/pipelines into a named dlt component. Use for
  ingest / extract-load goals — new sources, incremental refactors, adding a
  resource, or destination changes. It does NOT author dbt models or compose
  pipelines/<name>.toml; it emits dependency hints for those instead.
# model: omitted — falls back to the install default_model (spec 16)
tools: [edit, create_file, bash, grep, glob, web_fetch, sql, dlt_library, rest_api_explore, dbt_source_lookup, existing_dlt_inspect, "mcp:*"]
#   edit/create_file  — string-replace authoring (read-before-edit) + net-new files, spec 15
#   bash              — `dlt pipeline <name> info`/`trace` inspection + `pip`; component execution is via Carve's venv runner (spec 15)
#   grep/glob         — search the component + repo (find existing el/**/*.py for brownfield context)
#   web_fetch         — read a source API's live docs / OpenAPI spec
#   sql               — the dialect-aware sql tool (spec 18) — confirm the REAL destination schema, never guess
#   dlt_library       — list / lookup / copy the curated connector skill library
#   rest_api_explore  — bounded HTTP probing for API discovery
#   dbt_source_lookup — match against the user's dbt sources.yml
#   existing_dlt_inspect — read user-authored dlt for brownfield patterns
#   "mcp:*"           — any MCP-imported skill the user has allowed (spec 16)
allowed_paths: ["el/**", ".dlt/*.template"]   # write scope (gate-enforced, spec 15); never live .dlt/config.toml or secrets.toml
max_mode: build                               # the advisory-lint ceiling (spec 16)
classifications: [new_pipeline, modify_pipeline, refactor_pipeline_to_incremental, add_resource_to_pipeline, update_pipeline_destination]
---
<system prompt body — see "The system prompt" below>
```

- **Permission mode = `build`** (spec 15): the harness runs this subagent in `build` mode, so `edit`/`bash` are allowed **within `allowed_paths`**; `dlt pipeline <name> info`/`trace` inspection + read queries auto-allow; writes outside `allowed_paths`, `DROP`/DDL, and `git push` **prompt** (interactive) or **deny** (headless). The old per-agent `[guardrails]` (`forbidden_write_paths`, `allowed_write_paths`, `max_skill_calls_per_invocation`, `max_result_size_bytes`) are now the harness's job: `allowed_paths` above is the write scope the gate enforces; the forbidden absolute paths (`/`, `~/`, `/etc/`, …) fall out of "writes only within `allowed_paths`, everything else denied"; call/size caps are the harness's bounded-loop + skill-category caps (spec 15/16).
- **Tool grants are attenuated at runtime** (spec 15): the effective tool set is `grant ∩ mode-permitted`, enforced by the pre-execution gate on every call — in `build` every tool above is permitted, but the same file in `read_only` (if the orchestrator ever delegated it there) would have `edit`/`bash`-writes gated off. Spec 16's load-time check is an **advisory lint** (warns if a grant exceeds the agent's `max_mode`), not the security boundary.
- The agent **omits** the `model:` field, so it runs on the install's `default_model` resolved from `carve.toml`'s `[models]` block (or env var) per spec 16's per-agent tiering. A user (or a later tuning pass) can pin a tier by adding a `model:` line to the frontmatter; the engineer ships without one.

### The review subagents (dlt-qa + dlt-security)

> **Updated during implementation (2026-06-23):** the shipped reviewers carry **`max_mode: read_only`** specifically (the real permission-mode name, spec 15 — not an interchangeable "plan" mode) plus an empty **`allowed_paths: []`** (they report, never edit). The orchestrator-owned fan-out shipped as `core/agents/review_fan_out.py` — a `Finding`/`Severity`/`ReviewResult` model + `review_fan_out(diff, goal, delegate_fn) -> ReviewResult` that sequences dlt-qa→dlt-security over an **injected** `delegate_fn`, passes only `{diff, goal}`, parses findings **fail-loud**, stamps `reviewer` from the trusted call site, and sets `passed` = no `blocker`/`major`. It ships as a **wired-but-dormant seam**: the live orchestrator goal-routing that constructs the real `delegate_fn` is the deferred orchestrator-wiring unit (see Open questions).

The DLT engineer's diff does not ship straight to the user. It passes through two **review subagents** — the `/build-spec` engineer→parallel-reviewers→fix pattern Carve runs on *itself*, now brought to users' pipelines. Both are declarative agents (`builtin/dlt-qa.md`, `builtin/dlt-security.md`) run in the **`read_only`** permission mode (spec 15) on a **fresh, adversarial context** (they see the diff + the goal, not the engineer's transcript — context isolation, spec 15). Each returns a structured set of findings (severity + file:line + suggested change); the **orchestrator** (which owns the review fan-out — the engineer does not itself `delegate`, per spec 15's depth model: orchestrator → engineer, then orchestrator → reviewers as siblings) feeds findings back to the engineer, which iterates until the reviewers pass or a finding is surfaced to the user.

- **dlt-qa** (`tools: [grep, glob, sql, read_file]`) — reviews for correctness/quality the engineer's own loop can miss: schema-contract fit (does the resource match the declared/destination schema, via the `sql` tool), incremental-cursor correctness, idempotency / write-disposition sanity, requirements pinning, and convention/standards adherence (spec 06). It does **not** edit; it reports.
- **dlt-security** (`tools: [grep, glob, read_file]`) — reviews for safety: no live credentials baked into code or `.dlt/*.template` (only `${ENV}` placeholders), no secrets logged, write-disposition choices that risk data loss (`replace` on a table that should `merge`/`append`), and that `rest_api_explore` usage stayed within bounds (no write verbs, no exfiltration to non-source hosts). It does **not** edit; it reports.

This is framed, not exhaustively specified: the review fan-out mechanics (spawn, collect, feed-back) are the harness's (spec 15); the staged-start question — security-on-deploy + qa-on-build first, full fan-out later — is the strategy's review-fan-out open question. Carve wires **qa-on-build** (every authored diff) and **security-on-build** for credential/data-loss checks; deploy-time gating composes with spec 14.

### Context bundle (input from the orchestrator's `delegate`)

The DLT engineer is `delegate`d a task with a **context bundle** (spec 15 — the only context the subagent sees; not the parent's transcript). The bundle carries:

```python
{
  "goal_slice": "Generate a dlt pipeline that ingests the Stripe charges API into raw_stripe, incremental on created_at",
  "classification": "new_pipeline",                  # or modify_pipeline, etc.
  "component_name": "stripe_charges",                # the target dlt component; in simple mode this is the el/<name>/ directory it writes into
  "component_root": "el/stripe_charges/",            # resolved write root for the component, per spec 03 (el/<name>/ in simple mode; the cloned workspace path in separate-remote mode)
  "memory": {
    "conventions":    "...",                          # subset of carve/conventions.md
    "standards":      "...",                          # subset of carve/standards.md (full document; cheap)
    "el_notes":       "...",                          # optional, present only if the component's NOTES.md exists (el/<name>/NOTES.md in simple mode)
  },
  "destination": {
    "kind":           "snowflake",
    "schema":         "raw_stripe",
    "credentials_env": "DESTINATION__SNOWFLAKE__CREDENTIALS",
    "available_targets": ["dev", "prod"],
  },
  "existing_sources": [
    {"name": "stripe", "schema": "raw_stripe", "tables": ["charges", "customers"], "dbt_component": "analytics"},
  ],                                                  # from the dbt component's sources.yml (the detected project in simple mode; the cloned repo in separate-remote)
  "dlt_library_match": "stripe",                     # set when the orchestrator's library-lookup skill matched
  "dlt_library_match_confidence": "high",             # "high" | "medium" | "low" — orchestrator's heuristic
  "existing_components": [                             # other dlt components, for brownfield context
    {"name": "salesforce_accounts", "type": "dlt", "path": "el/salesforce_accounts/", "provenance": "user-authored"},
  ],
  "modification_target": null,                        # when modifying an existing pipeline, references the existing files
}
```

For `classification = "modify_pipeline"`, the orchestrator additionally provides:

```python
{
  "modification_target": {
    "pipeline_name": "stripe_charges",
    "existing_files": {
      "el/stripe_charges/__init__.py": "...",          # full current contents
      "el/stripe_charges/requirements.txt": "...",
    },
    "provenance": {"source": "carve-generated", "library_name": "stripe", "library_commit": "abc1234"},
    "user_modifications_below_header": True,           # whether user edits exist below the provenance header
  }
}
```

### The four authoring strategies

For each invocation, the agent picks **exactly one** strategy. Selection happens via the system-prompt body's decision tree, not via deterministic code — the LLM reasons about which to pick given the context, pulling in the matching strategy **skill pack** (`src/carve/core/skills/builtin/dlt_strategies/<strategy>/SKILL.md`, loaded on description-match per spec 16) for detailed guidance. The prompt enforces a hierarchy:

1. **Curated library copy** — if `dlt_library_match` is set with `high` confidence (e.g., the user's goal explicitly mentions Stripe and we have a curated Stripe source), apply the `<name>` connector skill pack: `dlt_library_copy` lays the curated source from `src/carve/sources/<name>/` into `el/<component_name>/`, the agent customizes for the destination/schema via `edit`, and sets provenance to library_name + commit.

2. **dlt REST API generic config** — if the source is a clean REST API (JSON responses, standard pagination, OAuth or bearer auth), emit a TOML config block describing endpoints + pagination + auth. Generates a thin `__init__.py` that loads the config and calls `dlt.sources.rest_api.rest_api_source(...)`. Most lightweight option.

3. **Native dlt source** — for sources that don't fit the REST API config (GraphQL, non-standard pagination, complex auth flows, streaming APIs, database CDC, file-based sources). Agent writes Python with `@dlt.source` + `@dlt.resource` decorators, handles pagination/auth/incremental cursors in code. Most flexible.

4. **Singer/Airbyte wrapper** — fallback when none of the above fit and a Singer tap exists for the source. Agent writes a thin Python wrapper invoking the tap via dlt's `dlt.sources.singer_pipeline.singer_source(...)` (or equivalent). Adds `tap-<name>` to `requirements.txt`. Used sparingly; the prompt steers away from this unless other strategies don't apply.

The prompt is explicit that REST API config is preferred over native dlt where it applies, and that curated library trumps all other strategies when applicable.

> **CDC scope note.** "database CDC" in strategy 3 means dlt's database-replication sources (e.g. Postgres `pg_replication`). SaaS CDC such as Salesforce Change Data Capture is **not** an initial target — dlt ships no SaaS CDC source, and the curated Salesforce source is cursor/`SystemModstamp`-based. CDC for SaaS sources is a later enhancement (see use-cases UC3).

### Outputs per strategy

For all four strategies the agent writes:

- `el/<pipeline_name>/__init__.py` — with the provenance header
- `el/<pipeline_name>/requirements.txt` — with pinned deps (`dlt[snowflake]==X.Y.Z` plus strategy-specific extras)
- `.dlt/config.toml.template` — additive merge with new entries for this pipeline's destination + source config
- `.dlt/secrets.toml.template` — additive merge with new entries for credential references

For curated library strategy, additionally:

- The provenance header records `library_name`, `library_commit`, and the destination customization

For REST API config strategy, additionally:

- `el/<pipeline_name>/rest_api_config.toml` (or inline in `__init__.py`; agent picks based on complexity)

The agent authors via the `edit` tool (string-replace, read-before-edit) and **runs** the result via `bash`; the permission gate (spec 15) confines every write to `allowed_paths`. The agent never writes:

- `.dlt/config.toml` or `.dlt/secrets.toml` directly (those are user-provided per environment; they're outside `allowed_paths`, so the gate denies it)
- Anything outside `el/<component_name>/` and the `.dlt/*.template` files (the gate denies; in interactive mode it prompts)
- A `pipelines/<name>.toml` (that's the pipeline engineer's job, spec 08)

### Output to the orchestrator (the delegation summary)

The subagent returns a **summary**, not its transcript (spec 15's `DelegationResult`: `result_summary`, `files_changed`, `outputs`, `cost_usd`, `status`). The DLT engineer fills `outputs` with the dlt-specific payload the orchestrator folds into the Plan — including the **verification result** (the agent ran the pipeline; this isn't a promise, it's an observed outcome) and the **review outcome**:

```python
# DelegationResult.outputs (the dlt-engineer-specific payload)
{
  "specialist": "dlt-engineer",
  "status": "completed",                  # or "needs_user_input" if the gate blocked something or a review finding needs the user
  "strategy_used": "curated_library",     # one of the four
  "files_changed": [                       # what the agent actually edited (rolls up to DelegationResult.files_changed)
    {"path": "el/stripe_charges/__init__.py", "hash": "sha256:...", "content_preview": "..."},
    {"path": "el/stripe_charges/requirements.txt", "hash": "sha256:..."},
    {"path": ".dlt/config.toml.template", "additions": "..."},
    {"path": ".dlt/secrets.toml.template", "additions": "..."},
  ],
  "verification": {                        # the verification loop's CheckResult (spec 15), parsed from dlt state.json
    "command": "run el/stripe_charges via venv runner; inspect via `dlt pipeline stripe_charges info`",
    "status": "green",                     # green after self-correction; or "needs_user_input" if a failure needs a human (e.g. missing creds)
    "iterations": 2,                       # how many generate→run→read→fix cycles it took
    "rows_loaded": {"charges": 1284},
  },
  "review": {                              # the dlt-qa + dlt-security fan-out result
    "qa": "passed",                        # or a list of unresolved findings
    "security": "passed",
  },
  "dependencies": {
    "dbt_sources_needed": [               # the pipeline engineer consumes this for source coupling
      {"source_name": "stripe", "table": "charges", "schema": "raw_stripe"},
    ],
    "destination_schemas_needed": ["raw_stripe"],
  },
  "expected_outputs": {                   # surfaced in the plan summary
    "tables_created": ["raw_stripe.charges"],
  },
  "tool_calls": [...],                     # the trace, recorded per ARCHITECTURE §9.5; cost/tokens roll up to agent_invocations
}
```

### The verification loop (close the loop with execution)

This is the harness's accuracy primitive (spec 15) applied to dlt, and the change that turns the agent from a generator into a colleague. After authoring (any strategy), the DLT engineer **does not stop at generation** — it runs the code and reads the real result:

1. **Run** the component's Python entrypoint via Carve's **venv runner** (`LocalVenvRunner` — the same structured primitive the runtime's dlt/python step uses) against a dev/test target (the verification loop uses the dev target + read/write roles per spec 18; never prod). Note: `dlt` ships **no** `run`/`check` CLI subcommand — a dlt pipeline *is* a runnable Python module — and freeform `python` is denied by the bash gate, so execution goes through the structured runner, **not** raw `bash`. (`dlt pipeline <name> info`/`trace` *is* available via `bash` for read-only inspection.)
2. **Read** the parsed `CheckResult` (spec 15's `run_check`, with the dlt parser at `src/carve/integrations/dlt/verify.py` that reads the on-disk load package's `state.json` for rows-loaded/schema-changes/errors — the verdict comes from the load package, not from the runner's exit code). The agent never invents what the run can report — and it uses the **`sql` tool** (spec 18) to confirm the *actual* destination schema the load produced, rather than guessing it.
3. **Fix** and re-run, bounded by the harness's attempt cap, until green. A failure it cannot fix itself (e.g. missing credentials, a source-side auth error) is summarized as `status = "needs_user_input"` with the grounded evidence, not silently shipped.

The agent grounds on real tool output throughout: a dlt exception, a `state.json` schema diff, or an `INFORMATION_SCHEMA` read via `sql` — never a hallucinated schema. (Recovery, spec 17, reuses exactly this machinery when it `delegate`s a fix to this agent.)

### Modification semantics

When `classification = "modify_pipeline"`, the agent:

1. Reads the existing `el/<name>/__init__.py` (provided in the context bundle, and re-readable via `read_file`/`grep` for the read-before-edit invariant)
2. Identifies what needs to change (a new resource, an incremental cursor, a different destination, a write disposition change)
3. Applies the minimal `edit` — a string-replace diff, not a regenerated file. The provenance header is preserved.
4. If user modifications exist below the header, the agent diffs against the previous build's expected content (recorded in `Build.manifest_json`) and either:
   - Merges cleanly (the user's edits don't conflict with the modification): applies the change, preserves user edits
   - Surfaces a conflict: the summary returns `status = "needs_user_input"` with the conflict surfaced to the user, who picks a resolution before build proceeds
5. **Re-verifies** via the loop above — a modification is run, not just diffed.

> The skills below are granted to the DLT engineer via its `tools:` frontmatter and bind to real executors through the harness **grant→executor binder** (spec 16's tool-binding seam) when the engineer's runtime tool set is composed — the same path as the base tools. They are implemented as **callable Tools** (path readers / a bounded HTTP probe / a curated-pack copier), not warehouse-coupled `@skill` functions: a domain skill that needs only the project tree or the network is a Tool the binder supplies, whereas the warehouse-coupled catalog skills (which need a live `SkillContext` with a Snowflake pool) stay `@skill` functions. Schema introspection is **not** among them: the agent reads the real destination schema through the dialect-aware **`sql` tool** (spec 18, `op="introspect"`), which supersedes the old `destination_schema_query` skill and runs on the read role.

### Skill: `dlt_library` (`list` / `lookup` / `copy`) — the connector skill library

The curated connector library *is* a skill library (spec 16): each `src/carve/sources/<name>/` ships as a `SKILL.md` pack. The `dlt_library` skill is the interface to it:

- **`dlt_library.list()`** → returns the list of curated source packs in `src/carve/sources/` with metadata (name, description, supported destinations, last updated)
- **`dlt_library.lookup(query: str)`** → fuzzy search across names + descriptions; returns top-5 with confidence scores. Used by the orchestrator during context-bundle assembly to set `dlt_library_match`.
- **`dlt_library.copy(name: str, dest_path: Path, customization: dict)`** → lays the curated source pack from `src/carve/sources/<name>/` into `<dest_path>`, applies customization (destination, schema, credentials env-var names), and writes the provenance header. Returns the list of files written. (The agent then customizes further via `edit` and verifies via the loop.)

**Per-source introspection skills.** Each curated source pack also exposes source-specific introspection skills — `<source>_list_objects()` and `<source>_describe_object(name)` (e.g. `salesforce_list_objects` / `salesforce_describe_object`). The orchestrator uses them during planning to confirm requested objects exist; **[recovery](./recovery.md) uses `describe_object` to read the current source schema for its schema diff.** They ship as part of the pack (alongside its `SKILL.md` + source code), not as standalone built-ins.

**Version adaptation.** The engineer adapts generated dlt code to the **detected dlt version** (resolved/pinned via [connect](./connect.md)) — e.g. avoiding APIs not present in the project's pinned dlt. (Detect-and-warn-on-out-of-range lives in [connect](./connect.md); adapting the *generated code* to the detected version is the engineer's.)

### Skill: `rest_api_explore`

A bounded HTTP exploration tool. Given a base URL and optional auth config, the skill makes a limited number of probing requests:

- `OPTIONS /` to discover allowed methods
- `GET /` or `GET /openapi.json` / `GET /swagger.json` to discover schema
- A small number of `GET /<endpoint>` sample requests to inspect response shape (with a configurable result-size cap)

Constraints:

- Maximum 20 requests per invocation
- Each request has a 10-second timeout
- Response bodies truncated at 50KB per response
- No POST/PUT/DELETE/PATCH ever
- The skill records each request in the tool-call trace for audit; URLs and (redacted) headers are surfaced in the plan trace. The **dlt-security** review subagent checks that exploration stayed within these bounds.

This skill is the agent's eyes for unfamiliar REST APIs (complementing `web_fetch` for human-readable docs). It's deliberately bounded to prevent the agent from accidentally hammering a user's production endpoint while exploring.

### Skill: `dbt_source_lookup`

> **Updated during implementation (2026-06-23):** the dbt-project resolution surface is the shipped `integrations/component_locator.py` (`_detect_dbt_project`, root + one-level-down), not a separate `integrations/dbt/locator.py` — that file was never created; dbt-project detection lives in the shared component locator.

Reads the user's dbt project's `sources.yml` files (per the [`integrations/component_locator.py`](./layout.md) `_detect_dbt_project` resolution from spec 03 — same-repo dbt project at `<root>/dbt_project.yml` or one level down). Exposes:

- `dbt_sources_list()` → all source declarations in the project
- `dbt_source_match(schema: str, table: str)` → does a source declaration exist for this schema+table? Returns the source's full config if so.

The orchestrator uses this when assembling the context bundle to populate `existing_sources`. The DLT engineer itself rarely calls it directly; the orchestrator hands it the relevant subset.

### Skill: `existing_dlt_inspect`

Reads existing dlt code in the user's `el/` directory (or the resolved dlt project path for separate-repo modes per spec 03):

- `dlt_existing_pipelines()` → list of existing `el/<name>/` directories with their provenance (carve-generated vs user-authored)
- `dlt_existing_pipeline_read(name)` → file contents of `el/<name>/__init__.py` and `requirements.txt`

Used when the DLT engineer needs to understand patterns in user-authored pipelines before generating a new one (e.g., the user's existing `salesforce_accounts` pipeline uses a specific auth pattern; a new `salesforce_contacts` pipeline should match). The agent can equivalently reach for `grep`/`glob` over `el/**` for ad-hoc pattern discovery.

### The system prompt (the agent-file body)

The system prompt is the **body of the declarative agent file** (`src/carve/core/agents/builtin/dlt-engineer.md`, below its frontmatter) — not a separate `prompts/*.md` path. Structure:

1. **Role** — "You are Carve's DLT engineer. Your job is to author *and verify* dlt code that pulls data from a source system and lands it in a destination warehouse. You architect as you build, and you do not consider your work done until the pipeline runs green."
2. **Key references** — dlt's documentation (reachable live via `web_fetch`), the connector skill library, the user's standards.md
3. **Inputs** — what the context bundle contains and how to use each field
4. **Tools & how to work** — author with `edit` (read-before-edit, minimal diffs), search with `grep`/`glob`, confirm the real destination schema with the `sql` tool, read live API docs with `web_fetch`/`rest_api_explore`, and **run** with `bash`. Stay within `allowed_paths`; never touch live `.dlt/config.toml`/`secrets.toml`.
5. **Strategy selection** — the four strategies, when to pick each, the hierarchy; pull the matching strategy **skill pack** for detail
6. **The verification loop** — after authoring, execute the component via Carve's venv runner (not raw `bash` — `dlt` has no `run`/`check` CLI and `python` is gate-denied; `dlt pipeline <name> info`/`trace` via `bash` is for inspection), read the parsed `CheckResult` from the load package, and iterate until green; ground every claim in real tool output; escalate (not fabricate) on failures you can't fix (missing creds, source auth)
7. **Code requirements** — provenance header, requirements.txt pinning, no live-credentials in templates, conventions to follow from standards.md and conventions.md
8. **Modification semantics** — how to handle classification="modify_pipeline", how to preserve user edits below the provenance header, re-verify after the change
9. **Review handoff** — your diff goes to the dlt-qa and dlt-security reviewers; expect findings and fix them before finishing
10. **Output format** — the delegation summary shape (status, strategy, files_changed, verification, review, dependency hints)
11. **Failure modes** — when to set `status = "needs_user_input"`, how to surface conflicts cleanly

Strategy-specific guidance lives in **skill packs** (`src/carve/core/skills/builtin/dlt_strategies/<strategy>/SKILL.md`) loaded on description-match (progressive disclosure, spec 16) rather than statically inlined — keeping the agent body lean while giving the LLM thorough strategy-specific detail only when a strategy is in play.

### Provenance header (recap from spec 03)

Every Carve-generated dlt file carries the header from [layout](./layout.md). The DLT engineer uses `src/carve/integrations/dlt/code_emitter.py` to ensure every file it writes is properly headered. The build step verifies headers are present in all expected files before the Build row transitions to `succeeded`; the dlt-qa reviewer flags a missing header as a finding.

## Tests

- **Unit (native dlt source):** given a goal of "ingest the Hacker News API top stories", agent emits an `__init__.py` with `@dlt.source` + `@dlt.resource`, pagination loop, correct destination config; provenance header present
- **Unit (REST API config):** given a goal of "ingest the GitHub issues API", agent emits a REST API config block + a thin `__init__.py` invoking `rest_api_source(...)`
- **Unit (curated library):** given `dlt_library_match = "stripe"` (assuming a curated Stripe source exists in `src/carve/sources/`), agent calls `dlt_library_copy`, customizes for the destination, sets provenance to the library commit
- **Unit (Singer wrapper):** given a goal involving a niche SaaS source matching a known Singer tap, agent emits the wrapper code + adds the tap to `requirements.txt`
- **Unit (modification):** given an existing `el/stripe_charges/__init__.py` and a goal of "make this incremental on `created_at`", agent emits a minimal diff (adds `dlt.sources.incremental` cursor + changes write_disposition), preserves the existing provenance header
- **Unit (modification with user edits):** existing pipeline has user edits below the provenance header; agent's modification cleanly merges (where possible) or surfaces a conflict
- **Unit (agent definition):** the built-in `dlt-engineer.md` parses (frontmatter + body) via the spec-16 loader; its `tools` grant is valid for `build` mode; a `carve/agents/dlt-engineer.md` override is picked up (spec 16 mechanics; smoke-tested here).
- **Unit (permission gate):** in `build` mode the agent's `edit`/`bash` writes succeed within `allowed_paths` (`el/**`, `.dlt/*.template`); an attempt to write outside (e.g. `~/.bashrc`, `carve/`, live `.dlt/config.toml`) is gated (prompt interactive / deny headless) per spec 15 — replacing the old per-agent guardrail test.
- **Integration (verification loop):** the agent authors a trivial dlt component, executes it via the venv runner, the harness parses the load package's `state.json` into a `CheckResult`; a deliberately-broken artifact (e.g. a bad cursor field) triggers a self-correction iteration to green; the agent confirms the loaded schema via the `sql` tool.
- **Integration (review fan-out):** the engineer's diff is routed through dlt-qa and dlt-security; an injected problem (a secret literal in `.dlt/secrets.toml.template`, or a `replace` disposition that should be `merge`) is flagged by the right reviewer and triggers a fix iteration before the summary returns `completed`.
- **Integration (end-to-end):** full plan → build → run cycle against a mock HTTP API (httpserver fixture); rows land in a test Snowflake or DuckDB destination; Carve's own assertions verify the structural shape of the loaded data
- **Integration (REST API explorer):** the rest_api_explore skill hits a controlled httpserver, makes the expected request shape, respects the 20-request cap and 50KB body truncation

## Acceptance

- The DLT engineer loads from the built-in `dlt-engineer.md` (spec-16 format), is `delegate`-routable by classification, runs in `build` mode, and returns a **summary** (not its transcript) to the orchestrator.
- For each of the four strategies, the agent produces a working dlt pipeline and — via the **verification loop** — actually runs it (executed via Carve's venv runner) against a test destination, self-correcting to green; the loaded schema is confirmed via the `sql` tool, never guessed.
- The diff passes through the **dlt-qa** and **dlt-security** reviewers; a credential-in-template or risky-write-disposition problem is caught and fixed before the change is surfaced.
- The strategy hierarchy is honored: curated library wins when available; REST API config beats native dlt for clean REST sources; native is the fallback for complex cases; Singer wrapper is rare
- Modifications produce minimal diffs; the provenance header survives; user edits below the header are preserved or surfaced for conflict resolution; the modification is re-verified
- The agent never writes outside its `allowed_paths` — the permission gate enforces it
- Every generated dlt file has the provenance header from spec 03
- The agent's invocation is recorded in `agent_invocations` with token counts, cost, duration, status (cost rolls up from the subagent to the parent)
- `carve plan "ingest the X API"` for X in {Stripe, GitHub issues, Hacker News, Salesforce} produces a coherent plan that the user can build/run; all four strategies are exercised across the four examples
- The full M1 test suite still passes (layering the DLT engineer onto the harness doesn't break the preserved loop)

## Design notes

- **Why a declarative agent (markdown) instead of the hardcoded `extract_load/` package?** This is the harness model's core unlock (spec 16) and it resolves the built-vs-spec agent drift: the DLT engineer becomes a file anyone can read, version, override, and share — same format as user agents — hot-reloaded, not recompiled. The class disappears; the harness loads the markdown and runs it as a subagent.
- **Why verify by execution (the loop) rather than ship generated code?** Generation-without-verification is a demo; generate→run→read→fix is a colleague (spec 15). a dlt pipeline *is a runnable Python entrypoint* — executing it (via Carve's venv runner) and reading the real load-package `state.json`/exception is what makes the agent accurate and self-correcting, and it grounds the agent so it can't ship a hallucinated schema. (dlt's `pipeline` CLI only *inspects* an already-run pipeline — `info`/`trace`/`show` — it has no `run` subcommand.) This is the single biggest accuracy gain in the revision.
- **Why dlt-qa + dlt-security review subagents?** Carve already runs the engineer→parallel-reviewers→fix pattern on *itself* (`/build-spec`); bringing it to users' pipelines catches the classes of error the author's own loop is weakest at — schema-contract/idempotency (qa) and credential/data-loss (security) — on a fresh, adversarial, context-isolated read. Reviewers report; the engineer fixes; quality compounds without bloating one prompt.
- **Why the agent body + strategy skill packs rather than one monolithic prompt?** One agent identity (the DLT engineer) that knows the full dlt API reasons best about strategy selection; the per-strategy *detail* lives in skill packs loaded on description-match (progressive disclosure, spec 16), so the agent's context stays lean and only pulls deep guidance for the strategy actually in play. The hierarchy is explicit in the body; the LLM justifies its choice in the summary trace.
- **Why the REST API config strategy specifically?** dlt's `rest_api_source` is a real production feature designed exactly for the LLM-scaffolding use case — many SaaS APIs fit a TOML config and need zero custom Python. Preferring it over native dlt minimizes the code Carve generates and reduces the surface area for the agent to make mistakes. The agent only falls back to native dlt when REST API config genuinely can't express the API's quirks.
- **Why is curated library above REST API config in the hierarchy?** Because a curated library entry has been hand-tuned for the specific source — it handles known quirks (rate limits, error patterns, schema evolution) that a from-scratch REST API config wouldn't know about. When we have a curated source pack, prefer it.
- **Why does the DLT engineer emit dependency hints rather than directly creating `pipelines/<name>.toml`?** Separation of concerns. The pipeline engineer (spec 08) owns pipeline composition (steps, dependencies, failure modes, schedules); the DLT engineer owns dlt code. The dependency hints (dbt sources needed, destination schemas needed) flow up to the orchestrator in the summary, which then `delegate`s the pipeline composition to the pipeline engineer with that context.
- **Why limit `rest_api_explore` to GETs and a 20-request cap?** Defense in depth. The agent should not be able to accidentally hammer a production endpoint, accidentally mutate state on a non-idempotent endpoint, or run away with exploration. 20 requests is enough to discover schema for most REST APIs; truly esoteric APIs require the user to provide more context manually (e.g., paste an OpenAPI spec into the goal description). The dlt-security reviewer double-checks the bound held.
- **Why the permission gate (spec 15) rather than a per-agent `[guardrails]` block?** The write-scope check belongs in one place across every agent (DLT, pipeline, dbt): `allowed_paths` + the `build`-mode allowlist, checked before each tool call, is the harness's job. It subsumes the old per-agent `forbidden_write_paths`/`allowed_write_paths`/call-caps and is also the trust story once agents run `bash` and touch the warehouse — powerful, but never free to do arbitrary things.

## Open questions

- **The curated library's first wave (Stripe, Salesforce, etc.).** *Strategy-required.* This spec ships only one reference curated source (`_reference_hackernews/`, now a skill pack) plus the framework. Which top-10 or top-20 SaaS sources to prioritize for the curated library is a separate decision needing user input — and per positioning #2's resolution, the heuristic is "most popular Airbyte sources dlt doesn't already have native coverage of." Defer to a follow-up workstream (a later increment).
- **Confidence-score heuristic for `dlt_library.lookup`.** *Implementation default.* Token-based name similarity + tag matching; threshold of 0.85 for "high" confidence. Tune in practice; the orchestrator's decision is based on the score so threshold changes shift strategy selection.
- **Should the DLT engineer ever delete files?** *Implementation default.* No. The `edit` tool is string-replace, not delete (spec 15). If a pipeline modification implies a file no longer makes sense (e.g., a strategy switch from native dlt to REST API config), the agent emits a "delete this file" hint in its summary; the build step performs the deletion under user review. Agents never delete directly.
- **How to handle credential discovery for new sources.** *Implementation default.* Agent emits `.dlt/secrets.toml.template` with placeholder env-var references (e.g., `STRIPE_API_KEY = "${STRIPE_API_KEY}"`). The build flow surfaces these as "you need to set these env vars before running" in the plan summary; a missing credential makes the verification loop return `status = "needs_user_input"` rather than fail silently. Agent never invents real credentials; the dlt-security reviewer asserts no literal secret leaked into a template.
- **Built-in declarative-agent path → RESOLVED.** Built-in agents live at `src/carve/core/agents/builtin/*.md` (spec 16's discovery root + file-list); user agents at `carve/agents/*.md`. Consistent across specs 15/16/17/18.
- **Review fan-out scope.** *Strategy-required (shared).* This spec wires **qa-on-build + security-on-build**; the strategy's open question on full fan-out vs a staged start (and deploy-time security gating via spec 14) is owned at the strategy/harness level. Confirm the initial cut for the dlt reviewers specifically.

- **Deferred orchestrator-wiring (the live-routing seam).** *Implementation default — non-blocking, owned by the later orchestrator-wiring unit, gated on the plan-build classifier producing a goal classification.* The agent definition, the four strategy packs, the callable skills, and the verification runner bridge all ship and pass their gates; what remains is the live wiring that constructs the runner and routes a real goal to this agent:
  - **(a) Register the strategy-pack root.** Add `src/carve/core/skills/builtin/dlt_strategies/` as a skill-pack discovery root so the four authoring-strategy packs (`curated_library` / `rest_api_config` / `native_dlt` / `singer_wrapper`) are findable by `lookup_skill_pack` at runtime.
  - **(b) Inject the agent's non-base grants + append `lookup_skill_pack`.** Supply the `sql` / `dlt_library` / `rest_api_explore` / `dbt_source_lookup` / `existing_dlt_inspect` grants via the binder's `extra_tools` seam (spec 16's grant→executor binder — the harness holds no dependency for these names), and **append `lookup_skill_pack`** to the delegated agent's tool set (it's referenced in the body but intentionally absent from the frontmatter grant).
  - **(c) Make `allowed_paths` load-bearing.** Thread `allowed_paths` through `delegation.py`'s `AgentPolicy` and add a **glob-aware write-path matcher** so `el/**` / `.dlt/*.template` is enforced by the gate, not just advisory. (Today `allowed_paths` is advisory — a pre-existing harness gap flagged by the security reviewer; tracked at the harness level.)
  - **(d) Wire the live venv-runner→agent execution path.** The runner *bridge* (`integrations/dlt/runner.py`: `make_dlt_parse_fn` / `run_dlt_check` / `make_dlt_verification_loop`) exists and is tested; wiring the live `LocalVenvRunner` execution into the agent's verification loop is deferred to this same unit.
  - **(e) Wire the live review fan-out.** The orchestrator-owned driver (`core/agents/review_fan_out.py`: `review_fan_out(diff, goal, delegate_fn)`) + the `dlt-qa` / `dlt-security` reviewer agents + the findings model ship and are unit-tested with a **stub** `delegate_fn`. The live wiring — the orchestrator constructing `delegate_fn` as a partial over the real `SubagentRunner.run`, binding dlt-qa's `sql` grant via the binder's `extra_tools` seam (the reviewers run at `read_only`, so their grants attenuate to read tools + `sql`-introspect), and driving the engineer↔reviewer fix loop until the reviewers pass — is deferred to this same orchestrator-wiring unit (blocked on the plan-build classifier).
