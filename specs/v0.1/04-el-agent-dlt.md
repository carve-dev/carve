# v0.1-04 — EL specialist agent: generates dlt code

> The wedge of v0.1. The EL specialist takes a goal slice from the orchestrator and produces working dlt code in `el/<pipeline_name>/`, picking among four authoring strategies (native dlt source, REST API generic config, curated library copy, Singer/Airbyte wrapper). Per [PRD §5.2](../PRD.md), [PRD §6.3 project memory](../PRD.md), [ARCHITECTURE §2.2 agent layer](../ARCHITECTURE.md), [ARCHITECTURE §5.1–5.4](../ARCHITECTURE.md), [ARCHITECTURE §5.8 curated library](../ARCHITECTURE.md), [ARCHITECTURE §10.2 dlt invocation](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 4](../PROJECT_PLAN.md). Replaces the archived [P1-04 extract-load agent](../_archive/pillar-1-extract-load/04-extract-load-agent.md), whose premise (agent authors bespoke Python with `executemany`/`MERGE`) was broken by the dlt-backend positioning.

## Status

- **Status:** Drafting
- **Depends on:** [v0.1-01 state-store-postgres](./01-state-store-postgres.md), [v0.1-03 flat-layout](./03-flat-layout.md)
- **Blocks:** [v0.1-05 init-rewrite](./05-init-rewrite.md), [v0.1-07 runtime](./07-runtime.md) (the `dlt` step type executes what this agent produces), [v0.1-08 multi-step-pipeline](./08-multi-step-pipeline.md)
- **Built on:** the orchestration agent and reasoning loop from M1 (HISTORICAL — preserved, not rewritten). This spec adds the EL specialist on top.

## Goal

Ship the EL specialist agent that authors dlt code. Concretely:

- Receives pre-scoped context from the orchestrator (goal slice, destination, existing sources, memory files, optional curated-library match, optional brownfield pipeline references)
- Picks one of four authoring strategies based on the goal and context
- Writes dlt files into `el/<pipeline_name>/` plus the relevant `.dlt/config.toml.template` / `.dlt/secrets.toml.template` entries
- Records provenance headers per [v0.1-03](./03-flat-layout.md)
- Returns a structured plan task to the orchestrator: files written, their hashes, expected outputs, dependencies on dbt sources
- Operates idempotently on modifications: re-running against an existing pipeline diffs cleanly, preserves user edits below the provenance header

This spec ships the agent definition, system prompt, the skills the agent calls, and the initial curated source library structure. The first wave of curated sources (Stripe, Salesforce, etc.) is a separate post-v0.1 effort; this spec ships the framework that those slot into.

## Out of scope

- The orchestration agent's classification + specialist-picking logic — that's part of M1's reasoning loop (HISTORICAL). This spec produces an EL specialist that the orchestrator routes work to; it doesn't change the orchestrator.
- The dbt specialist agent — that's v0.2 (Pillar 3).
- The pipeline composition step (`pipelines/<name>.toml`) — written by the runtime specialist, covered in [v0.1-08 multi-step-pipeline](./08-multi-step-pipeline.md). The EL agent emits a structured "this pipeline needs to be composed with these dependencies" hint that the orchestrator then routes to the runtime specialist.
- The actual contents of the curated source library beyond a small reference example (e.g., a "Hacker News API" sample). Curating the top-30 Airbyte ports is a separate workstream after v0.1.
- The CLI/REST/MCP surface for invoking the agent directly (e.g., `carve agents test extract-load`). That's part of [v0.1-09 rest-api](./09-rest-api.md) and [v0.1-10 mcp-server](./10-mcp-server.md).
- Orchestration-only mode (PRD §6.2 mode 2). When the user has existing dlt code, the orchestrator does NOT route to the EL agent for that pipeline — it routes to the runtime specialist to compose the existing artifact. This spec handles modes 1 (authoring) and 3 (mix); mode 2 is a no-op for this agent.

## Files this spec produces

```
carve/agents/extract-load.toml                          # NEW — built-in agent definition shipped with Carve
src/carve/core/agents/extract_load.py                   # NEW — agent class (loads prompt, calls SDK loop, validates outputs)
src/carve/core/agents/prompts/extract_load.md           # NEW — the system prompt (single consolidated prompt; supersedes M2-03's bespoke-Python prompts)
src/carve/core/agents/prompts/extract_load_strategies/  # NEW — strategy-specific prompt fragments included by reference
    native_dlt_source.md
    rest_api_config.md
    curated_library.md
    singer_wrapper.md
src/carve/core/skills/dlt_library.py                    # NEW — list, lookup, copy; uses src/carve/sources/
src/carve/core/skills/rest_api_explorer.py              # NEW — bounded HTTP HEAD/GET/OPTIONS probing for API discovery
src/carve/core/skills/dbt_source_lookup.py              # NEW — search the user's dbt sources.yml for matches
src/carve/core/skills/existing_dlt_inspect.py           # NEW — read existing user-authored dlt code for brownfield context
src/carve/core/skills/file_io.py                        # MODIFY (if exists from M1) — extend write guardrail with el/<name>/ scope
src/carve/integrations/dlt/code_emitter.py              # NEW — utilities the agent uses to emit valid dlt code with provenance header
src/carve/integrations/dlt/requirements_writer.py       # NEW — manage requirements.txt with pinned deps
src/carve/integrations/dlt/dlt_config_merger.py         # NEW — additive merges into .dlt/config.toml.template per-destination
src/carve/sources/README.md                             # NEW — explains the curated library layout and how to contribute
src/carve/sources/_reference_hackernews/                # NEW — one reference curated source for tests and docs
    __init__.py
    requirements.txt
    README.md
tests/unit/test_el_agent_native.py                      # NEW
tests/unit/test_el_agent_rest_api_config.py             # NEW
tests/unit/test_el_agent_curated_library.py             # NEW
tests/unit/test_el_agent_singer_wrapper.py              # NEW
tests/unit/test_el_agent_modification.py                # NEW — re-running against existing pipeline preserves user edits
tests/integration/test_el_agent_end_to_end.py           # NEW — full plan/build cycle against a mock HTTP API
tests/fixtures/mock_apis/                               # NEW — vcr-recorded or httpserver-based fixtures
docs/extract-load-agent.md                              # NEW — user-facing reference: what the agent does, the four strategies, how to influence them via standards.md
```

## Behavior

### Agent definition (`carve/agents/extract-load.toml`)

```toml
name = "extract-load"
model = "claude-{LATEST_SONNET}"
system_prompt_path = "src/carve/core/agents/prompts/extract_load.md"
max_tokens = 16384
max_iterations = 30

allowed_skills = [
  "read_file",
  "write_file",          # scoped to el/<name>/, .dlt/*.template via guardrail (spec 03)
  "list_files",
  "dlt_library_list",
  "dlt_library_lookup",
  "dlt_library_copy",
  "rest_api_explore",
  "destination_schema_query",
  "dbt_source_lookup",
  "existing_dlt_inspect",
  "mcp:*",               # any MCP-imported skill the user has allowed
]

[guardrails]
forbidden_write_paths = [
  "/",                   # absolute paths anywhere outside project
  "~/",                  # home directory
  "/etc/", "/usr/", "/var/", "/opt/",
]
allowed_write_paths = [
  "el/**",
  ".dlt/*.template",     # never the live .dlt/config.toml or .dlt/secrets.toml
]
max_skill_calls_per_invocation = 50
max_result_size_bytes = 51200

[specialization]
classifications = [
  "new_pipeline",
  "modify_pipeline",
  "refactor_pipeline_to_incremental",
  "add_resource_to_pipeline",
  "update_pipeline_destination",
]
```

`{LATEST_SONNET}` is interpolated at build time from `carve.toml`'s `[models]` block (or env var). v0.1 ships with the current Claude Sonnet as the default; users can override per-agent.

### Pre-scoped context (input from orchestrator)

The EL agent expects:

```python
{
  "goal_slice": "Generate a dlt pipeline that ingests the Stripe charges API into raw_stripe, incremental on created_at",
  "classification": "new_pipeline",                  # or modify_pipeline, etc.
  "pipeline_name": "stripe_charges",                 # the el/<name>/ directory it'll write to
  "memory": {
    "conventions":    "...",                          # subset of carve/conventions.md
    "standards":      "...",                          # subset of carve/standards.md (full document; cheap)
    "el_notes":       "...",                          # optional, present only if el/<name>/NOTES.md exists
  },
  "destination": {
    "kind":           "snowflake",
    "schema":         "raw_stripe",
    "credentials_env": "DESTINATION__SNOWFLAKE__CREDENTIALS",
    "available_targets": ["dev", "prod"],
  },
  "existing_sources": [
    {"name": "stripe", "schema": "raw_stripe", "tables": ["charges", "customers"], ...},
  ],                                                  # from dbt sources.yml in same-repo or remote-cached dbt project
  "dlt_library_match": "stripe",                     # set when the orchestrator's library-lookup skill matched
  "dlt_library_match_confidence": "high",             # "high" | "medium" | "low" — orchestrator's heuristic
  "existing_el_artifacts": [                          # for brownfield context
    {"name": "salesforce_accounts", "path": "el/salesforce_accounts/", "provenance": "user-authored"},
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

For each invocation, the agent picks **exactly one** strategy. Selection happens via the system prompt's decision tree, not via deterministic code — the LLM reasons about which to pick given the context. The prompt enforces a hierarchy:

1. **Curated library copy** — if `dlt_library_match` is set with `high` confidence (e.g., the user's goal explicitly mentions Stripe and we have a curated Stripe source), copy from `src/carve/sources/<name>/` into `el/<pipeline_name>/`, customize for the destination/schema, set provenance to library_name + commit.

2. **dlt REST API generic config** — if the source is a clean REST API (JSON responses, standard pagination, OAuth or bearer auth), emit a TOML config block describing endpoints + pagination + auth. Generates a thin `__init__.py` that loads the config and calls `dlt.sources.rest_api.rest_api_source(...)`. Most lightweight option.

3. **Native dlt source** — for sources that don't fit the REST API config (GraphQL, non-standard pagination, complex auth flows, streaming APIs, database CDC, file-based sources). Agent writes Python with `@dlt.source` + `@dlt.resource` decorators, handles pagination/auth/incremental cursors in code. Most flexible.

4. **Singer/Airbyte wrapper** — fallback when none of the above fit and a Singer tap exists for the source. Agent writes a thin Python wrapper invoking the tap via dlt's `dlt.sources.singer_pipeline.singer_source(...)` (or equivalent). Adds `tap-<name>` to `requirements.txt`. Used sparingly; the prompt steers away from this unless other strategies don't apply.

The prompt is explicit that REST API config is preferred over native dlt where it applies, and that curated library trumps all other strategies when applicable.

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

The agent never writes:

- `.dlt/config.toml` or `.dlt/secrets.toml` directly (those are user-provided per environment)
- Anything outside `el/<pipeline_name>/` and the `.dlt/*.template` files
- A `pipelines/<name>.toml` (that's the runtime specialist's job)

### Output to the orchestrator

The agent returns a structured Task result the orchestrator includes in the Plan:

```python
{
  "task_id": "el-stripe-charges-001",
  "specialist": "extract-load",
  "status": "completed",                  # or "needs_user_input" if a guardrail blocked something
  "strategy_used": "curated_library",     # one of the four
  "files_to_write": [
    {"path": "el/stripe_charges/__init__.py", "hash": "sha256:...", "content_preview": "..."},
    {"path": "el/stripe_charges/requirements.txt", "hash": "sha256:..."},
  ],
  "config_files_to_merge": [
    {"path": ".dlt/config.toml.template", "additions": "..."},
    {"path": ".dlt/secrets.toml.template", "additions": "..."},
  ],
  "dependencies": {
    "dbt_sources_needed": [               # the runtime specialist consumes this for source coupling
      {"source_name": "stripe", "table": "charges", "schema": "raw_stripe"},
    ],
    "destination_schemas_needed": ["raw_stripe"],
  },
  "expected_outputs": {                   # surfaced in the plan summary
    "rows_loaded": "unknown (incremental; first run will backfill)",
    "tables_created": ["raw_stripe.charges"],
  },
  "skill_calls": [...],                    # the full trace, recorded in skill_calls table per ARCHITECTURE §9.5
}
```

### Modification semantics

When `classification = "modify_pipeline"`, the agent:

1. Reads the existing `el/<name>/__init__.py` (provided in pre-scoped context)
2. Identifies what needs to change (a new resource, an incremental cursor, a different destination, a write disposition change)
3. Emits the minimal diff — not a regenerated file. The provenance header is preserved.
4. If user modifications exist below the header, the agent diffs against the previous build's expected content (recorded in `Build.manifest_json`) and either:
   - Merges cleanly (the user's edits don't conflict with the modification): applies the change, preserves user edits
   - Surfaces a conflict: the plan task returns `status = "needs_user_input"` with the conflict surfaced to the user, who picks a resolution before build proceeds

### Skill: `dlt_library_list` / `dlt_library_lookup` / `dlt_library_copy`

- **`dlt_library_list()`** → returns the list of curated sources in `src/carve/sources/` with metadata (name, description, supported destinations, last updated)
- **`dlt_library_lookup(query: str)`** → fuzzy search across names + descriptions; returns top-5 with confidence scores. Used by the orchestrator during pre-scoping to set `dlt_library_match`.
- **`dlt_library_copy(name: str, dest_path: Path, customization: dict)`** → copies the source files from `src/carve/sources/<name>/` into `<dest_path>`, applies customization (destination, schema, credentials env-var names), and writes the provenance header. Returns the list of files written.

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
- The skill records each request in `skill_calls.payload` for audit; URLs and (redacted) headers are surfaced in the plan trace

This skill is the agent's eyes for unfamiliar REST APIs. It's deliberately bounded to prevent the agent from accidentally hammering a user's production endpoint while exploring.

### Skill: `dbt_source_lookup`

Reads the user's dbt project's `sources.yml` files (per the [`integrations/dbt/locator.py`](./03-flat-layout.md) resolution from spec 03). Exposes:

- `dbt_sources_list()` → all source declarations in the project
- `dbt_source_match(schema: str, table: str)` → does a source declaration exist for this schema+table? Returns the source's full config if so.

The orchestrator uses this in pre-scoping to populate `existing_sources` in the agent's context. The EL agent itself rarely calls it directly; the orchestrator hands it the relevant subset.

### Skill: `existing_dlt_inspect`

Reads existing dlt code in the user's `el/` directory (or the resolved dlt project path for separate-repo modes per spec 03):

- `dlt_existing_pipelines()` → list of existing `el/<name>/` directories with their provenance (carve-generated vs user-authored)
- `dlt_existing_pipeline_read(name)` → file contents of `el/<name>/__init__.py` and `requirements.txt`

Used when the EL agent needs to understand patterns in user-authored pipelines before generating a new one (e.g., the user's existing `salesforce_accounts` pipeline uses a specific auth pattern; a new `salesforce_contacts` pipeline should match).

### The system prompt

Single consolidated prompt at `src/carve/core/agents/prompts/extract_load.md`. Structure:

1. **Role** — "You are Carve's extract-load specialist. Your job is to author dlt code that pulls data from a source system and lands it in a destination warehouse."
2. **Key references** — dlt's documentation, the curated library, the user's standards.md
3. **Inputs** — what the pre-scoped context contains and how to use each field
4. **Strategy selection** — the four strategies, when to pick each, the hierarchy
5. **Code requirements** — provenance header, requirements.txt pinning, no live-credentials in templates, conventions to follow from standards.md and conventions.md
6. **Modification semantics** — how to handle classification="modify_pipeline", how to preserve user edits below the provenance header
7. **Output format** — the structured Task result shape
8. **Failure modes** — when to set `status = "needs_user_input"`, how to surface conflicts cleanly

Strategy-specific prompt fragments live in `src/carve/core/agents/prompts/extract_load_strategies/` and get included by reference. This keeps the main prompt under a reasonable size while still giving the LLM thorough strategy-specific guidance.

### Provenance header (recap from spec 03)

Every Carve-generated dlt file carries the header from [v0.1-03](./03-flat-layout.md). The EL agent uses `src/carve/integrations/dlt/code_emitter.py` to ensure every file it writes is properly headered. The build step verifies headers are present in all expected files before the Build row transitions to `succeeded`.

## Tests

- **Unit (native dlt source):** given a goal of "ingest the Hacker News API top stories", agent emits an `__init__.py` with `@dlt.source` + `@dlt.resource`, pagination loop, correct destination config; provenance header present
- **Unit (REST API config):** given a goal of "ingest the GitHub issues API", agent emits a REST API config block + a thin `__init__.py` invoking `rest_api_source(...)`
- **Unit (curated library):** given `dlt_library_match = "stripe"` (assuming a curated Stripe source exists in `src/carve/sources/`), agent calls `dlt_library_copy`, customizes for the destination, sets provenance to the library commit
- **Unit (Singer wrapper):** given a goal involving a niche SaaS source matching a known Singer tap, agent emits the wrapper code + adds the tap to `requirements.txt`
- **Unit (modification):** given an existing `el/stripe_charges/__init__.py` and a goal of "make this incremental on `created_at`", agent emits a minimal diff (adds `dlt.sources.incremental` cursor + changes write_disposition), preserves the existing provenance header
- **Unit (modification with user edits):** existing pipeline has user edits below the provenance header; agent's modification cleanly merges (where possible) or surfaces a conflict
- **Unit (guardrail enforcement):** agent attempts to write outside `el/<name>/` → guardrail rejects; agent's output reflects the rejection
- **Integration (end-to-end):** full plan → build → run cycle against a mock HTTP API (httpserver fixture); rows land in a test Snowflake or DuckDB destination; Carve's own assertions verify the structural shape of the loaded data
- **Integration (REST API explorer):** the rest_api_explore skill hits a controlled httpserver, makes the expected request shape, respects the 20-request cap and 50KB body truncation

## Acceptance

- For each of the four strategies, the agent produces a working dlt pipeline that passes `dlt pipeline check` and successfully runs against a test destination
- The strategy hierarchy is honored: curated library wins when available; REST API config beats native dlt for clean REST sources; native is the fallback for complex cases; Singer wrapper is rare
- Modifications produce minimal diffs; the provenance header survives; user edits below the header are preserved or surfaced for conflict resolution
- The agent never writes outside its allowed paths
- Every generated dlt file has the provenance header from spec 03
- The agent's invocation is recorded in `agent_invocations` with token counts, cost, duration, status
- `carve plan "ingest the X API"` for X in {Stripe, GitHub issues, Hacker News, Salesforce} produces a coherent plan that the user can build/run; all four strategies are exercised across the four examples
- The full M1 test suite still passes (the EL specialist doesn't break the M1 reasoning loop)

## Design notes

- **Why a single consolidated prompt rather than per-strategy prompts as the primary entry point?** Per resolved audit Q6 ("agent prompt content"): one prompt that knows the full dlt API gives the LLM the broadest context to reason about strategy selection. Per-strategy prompts as included fragments give detail without fragmenting the agent's identity into four sub-agents. The hierarchy is explicit in the prompt; the LLM is asked to justify its strategy choice in the Task result trace.
- **Why the REST API config strategy specifically?** dlt's `rest_api_source` is a real production feature designed exactly for the LLM-scaffolding use case — many SaaS APIs fit a TOML config and need zero custom Python. Preferring it over native dlt minimizes the code Carve generates and reduces the surface area for the agent to make mistakes. The agent only falls back to native dlt when REST API config genuinely can't express the API's quirks.
- **Why is curated library above REST API config in the hierarchy?** Because a curated library entry has been hand-tuned for the specific source — it handles known quirks (rate limits, error patterns, schema evolution) that a from-scratch REST API config wouldn't know about. When we have a curated source, prefer it.
- **Why does the EL agent emit dependency hints rather than directly creating `pipelines/<name>.toml`?** Separation of concerns. The runtime specialist owns pipeline composition (steps, dependencies, failure modes, schedules); the EL specialist owns dlt code. The dependency hints (dbt sources needed, destination schemas needed) flow up to the orchestrator, which routes the pipeline composition task to the runtime specialist with that context.
- **Why limit `rest_api_explore` to GETs and a 20-request cap?** Defense in depth. The agent should not be able to accidentally hammer a production endpoint, accidentally mutate state on a non-idempotent endpoint, or run away with exploration. 20 requests is enough to discover schema for most REST APIs; truly esoteric APIs require the user to provide more context manually (e.g., paste an OpenAPI spec into the goal description).
- **Why split file_io into a separate skill rather than inlining writes?** Because the file-write guardrail (spec 03) is shared with other agents (runtime specialist, dbt specialist in v0.2). A single guardrail-aware skill keeps the guardrail logic in one place.

## Open questions

- **The curated library's first wave (Stripe, Salesforce, etc.).** *Strategy-required.* This spec ships only one reference curated source (`_reference_hackernews/`) plus the framework. Which top-10 or top-20 SaaS sources to prioritize for the curated library is a separate decision needing user input — and per positioning #2's resolution, the heuristic is "most popular Airbyte sources dlt doesn't already have native coverage of." Defer to a follow-up workstream after v0.1 ships.
- **Confidence-score heuristic for `dlt_library_lookup`.** *Implementation default.* Token-based name similarity + tag matching; threshold of 0.85 for "high" confidence. Tune in practice; the orchestrator's decision is based on the score so threshold changes shift strategy selection.
- **Should the EL agent ever delete files?** *Implementation default.* No. If a pipeline modification implies a file no longer makes sense (e.g., a strategy switch from native dlt to REST API config), the agent emits a "delete this file" hint in the Task result; the build step performs the deletion under user review. Agents never delete directly.
- **How to handle credential discovery for new sources.** *Implementation default.* Agent emits `.dlt/secrets.toml.template` with placeholder env-var references (e.g., `STRIPE_API_KEY = "${STRIPE_API_KEY}"`). The deploy/build flow surfaces these as "you need to set these env vars before running" in the plan summary. Agent never invents real credentials.
