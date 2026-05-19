# v0.1-12 — `carve ask`: read-only investigative queries

> The fifth lifecycle verb (sibling to plan/build/run/deploy), strictly side-effect-free. Same orchestration agent and skills as `plan`, different output shape: a markdown answer with cited entities instead of a Plan. Per [PRD §6.5 ask](../PRD.md), [PRD §3 core loop sibling-verb note](../PRD.md), [ARCHITECTURE §7.1 ask](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 12](../PROJECT_PLAN.md).

## Status

- **Status:** Drafting
- **Depends on:** [v0.1-01 state-store-postgres](./01-state-store-postgres.md), [v0.1-06 project-memory](./06-project-memory.md) (asks cite `decisions.md` entries), [v0.1-09 rest-api](./09-rest-api.md) (this spec adds the asks router to the existing FastAPI app)
- **Soft depends on:** [v0.1-04 el-agent-dlt](./04-el-agent-dlt.md) for the orchestration-agent baseline; M1's reasoning loop (HISTORICAL); spec 07/08 for run/pipeline data the agent queries when answering
- **Blocks:** nothing structurally

## Goal

Add the `ask` verb as a read-only path through the orchestration agent. Concretely:

1. **Reuse the orchestration agent and skill registry** — no new specialist agent. The orchestrator is configured with an alternative system prompt + an extra guardrail that forbids any write skill.
2. **Ask data model**: `asks` table for indexing + `.carve/asks/<id>.json` for durability
3. **Answer format**: markdown text + cited entities (structured references into lineage / decisions / pipeline files) + the full skill-call trace
4. **CLI**: `carve ask "<question>"`, `carve asks list`, `carve asks show <id>`
5. **REST router** added to the spec-09 FastAPI app: `POST /api/v1/asks`, `GET /api/v1/asks/{id}`, `GET /api/v1/asks`
6. **MCP tool** auto-generated per spec 10 (tool name `ask`)
7. **Concurrency**: asks run in parallel with each other and with plan/build/run/deploy. No queue lock; no rows in the jobs table.

After this spec lands, a user can ask "where do we calculate net revenue?" or "why did we pick 18-month retention for Stripe?" and get a cited answer in 5–15 seconds — without anything in the project changing.

## Out of scope

- A new specialist agent (deliberately reuses the orchestration agent with a different prompt + guardrail)
- Streaming the answer token-by-token (synchronous return in v0.1; the answer arrives as one chunk when the orchestrator finishes)
- Asking *about* asks (e.g., "what have we been asking lately?") — handled by `carve asks list` rather than a meta-ask
- Embedding-based semantic search over decisions/code (post-v0.1; v0.1 ask uses the same skill stack as plan)
- Conversational follow-ups (each ask is a single round; multi-turn chat lives in the chat tool, e.g., Claude Desktop, which can call `ask` repeatedly)

## Files this spec produces

```
src/carve/core/agents/orchestration_ask_mode.py         # NEW — orchestrator invocation with the ask prompt + write-skill guardrail
src/carve/core/agents/prompts/orchestrator_ask.md       # NEW — system prompt for the ask path

src/carve/core/asks/__init__.py
src/carve/core/asks/store.py                            # NEW — persistence (.carve/asks/<id>.json + asks table index)
src/carve/core/asks/models.py                           # NEW — Ask, Answer, CitedEntity dataclasses
src/carve/core/asks/citation_builder.py                 # NEW — converts agent-mentioned references into structured CitedEntity objects

src/carve/cli/ask.py                                    # NEW — `carve ask`, `carve asks list`, `carve asks show` commands

src/carve/api/routers/asks.py                           # NEW — wires asks endpoints into the spec-09 app

src/carve/core/state/models.py                          # MODIFY — add Ask table
migrations/versions/0011_asks.py                        # NEW

tests/unit/test_ask_orchestrator_no_write_skills.py     # NEW — verifies guardrail rejection of write skills during ask
tests/unit/test_ask_citation_builder.py                 # NEW
tests/integration/test_ask_end_to_end.py                # NEW — real ask against a fixture project; verify answer + citations
tests/integration/test_ask_no_side_effects.py           # NEW — files / state store / destination unchanged after ask
tests/integration/test_ask_concurrent_with_plan.py      # NEW — ask runs alongside plan/build/run without contention
tests/integration/test_ask_cites_decisions.py           # NEW — "why did we do X?" surfaces decisions.md entries

docs/ask.md                                             # NEW — what ask does, example questions, how it differs from plan
```

## Behavior

### Ask data model

`src/carve/core/asks/models.py`:

```python
@dataclass(frozen=True)
class CitedEntity:
    kind: Literal[
        "decision",                # an entry in carve/decisions.md
        "dbt_model",
        "dbt_source",
        "dlt_pipeline",            # an el/<name>/ directory
        "pipeline_toml",           # a pipelines/<name>.toml
        "warehouse_table",
        "memory_file",             # conventions.md, standards.md, decisions.md, or a sidecar
        "skill_call",              # a specific skill_calls row used to produce this part of the answer
    ]
    identifier: str                # e.g., "stripe", "stg_orders", "el/stripe_charges/"
    excerpt: str                   # the relevant slice of content (≤ 500 chars)
    source_url: str                # link the static UI / cloud UI uses to deep-link

@dataclass(frozen=True)
class Answer:
    markdown: str
    cited_entities: list[CitedEntity]

@dataclass(frozen=True)
class Ask:
    id: UUID
    question: str
    pipeline: Optional[str]
    target: Optional[str]
    status: Literal["pending", "succeeded", "failed", "cancelled"]
    answer: Optional[Answer]                  # populated when status == "succeeded"
    error_message: Optional[str]              # populated when status == "failed"
    tokens_input: int
    tokens_output: int
    cost_usd: float
    duration_ms: int
    skill_call_trace_path: Path               # .carve/asks/<id>.json
    created_at: datetime
    finished_at: Optional[datetime]
    tenant_id: int
```

`asks` table schema (migration 0011):

```sql
CREATE TABLE asks (
  id UUID PRIMARY KEY,
  question TEXT NOT NULL,
  pipeline TEXT,
  target TEXT,
  status TEXT NOT NULL,
  answer_markdown TEXT,                    -- inline for quick listing; full structured data in JSON file
  cited_count INTEGER,                     -- for listing/filtering
  error_message TEXT,
  tokens_input INTEGER NOT NULL DEFAULT 0,
  tokens_output INTEGER NOT NULL DEFAULT 0,
  cost_usd NUMERIC(10, 4) NOT NULL DEFAULT 0,
  duration_ms INTEGER,
  skill_call_trace_path TEXT NOT NULL,     -- relative path under .carve/asks/
  tenant_id BIGINT NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX ix_asks_created_at ON asks(tenant_id, created_at DESC);
CREATE INDEX ix_asks_pipeline ON asks(pipeline) WHERE pipeline IS NOT NULL;
```

The full structured answer + skill-call trace is persisted to `.carve/asks/<id>.json` for durability and offline-readability. The DB row is the index.

### Orchestrator in ask mode

`src/carve/core/agents/orchestration_ask_mode.py`:

```python
async def run_ask(
    *,
    question: str,
    pipeline: Optional[str],
    target: Optional[str],
    orchestrator: Orchestrator,           # the same M1-shipped orchestrator
    memory_loader: MemoryLoader,
    state_store: StateStore,
) -> Ask:
    ask_id = uuid4()
    ask = Ask(id=ask_id, question=question, pipeline=pipeline, target=target, status="pending", ...)
    await state_store.asks.create(ask)
    try:
        # Compose pre-scoped context using the same machinery as plan, with is_investigative=True
        context = compose_ask_context(
            question=question,
            pipeline=pipeline,
            target=target,
            memory_loader=memory_loader,
            is_investigative=True,         # ensures decisions.md is included (spec 06's selector)
        )
        invocation = await orchestrator.invoke(
            system_prompt_path="src/carve/core/agents/prompts/orchestrator_ask.md",
            user_message=question,
            context=context,
            guardrails=[NoWriteSkillsGuardrail()],
            max_iterations=20,
        )
        answer = build_answer_from_invocation(invocation)
        return await state_store.asks.complete_succeeded(ask_id, answer, invocation.usage)
    except OrchestratorError as e:
        return await state_store.asks.complete_failed(ask_id, str(e))
```

Key behaviors:

- **The same orchestration agent** as `plan`. Configuration differences only: alternate system prompt, an extra guardrail (`NoWriteSkillsGuardrail`), a different output post-processor (builds `Answer` instead of `Plan`).
- **Same skill set** (catalog queries, manifest queries, lineage traversal, memory reads, grep, MCP-imported read skills). The guardrail layer filters writes.
- **No specialist dispatch.** Plan invocations delegate to specialists (EL, runtime, dbt-in-v0.2); ask does not — the orchestrator produces the answer itself from its skill-gathered context.

### The `NoWriteSkillsGuardrail`

Inspects every `tool_use` before the skill is invoked. Rejects skills whose names match:

- `write_file`, `write_file_*` (any variant)
- `dlt_library_copy` (writes files)
- `*_create`, `*_update`, `*_delete`, `*_remove` (resource-mutating verbs on the REST adapter — agent_create, pipeline_create, etc.)
- `mcp:*` calls whose tool schema declares `effects.writes = true` (MCP servers SHOULD declare their tool effects in the schema; if not, the guardrail allows by default — write-effect detection is best-effort for MCP tools and warned in the trace)

Rejected tool uses produce a tool_error returned to the LLM with message "ask is read-only; skill <name> is a write skill and was blocked." The LLM can then pick a different approach. Repeated guardrail rejections (more than 3 in one invocation) escalate to "ask cannot be answered without write skills" and the ask completes as `failed` with that message.

### System prompt: `orchestrator_ask.md`

Distinct from the plan prompt, with the following structure:

1. **Role** — "You answer the user's question about their project using only read skills."
2. **Inputs** — what's in pre-scoped context (memory, project state, target)
3. **Tools** — the read skills available; explicit reminder that write skills will be blocked
4. **Answer format** — markdown body + a list of citations (one per claim with non-obvious provenance). Citations are inline as Markdown footnotes `[^1]` with the entity references in a structured tail block the citation_builder parses.
5. **Style** — concise, factual, no apologies for not being able to do things. If the project doesn't have the information, say so directly: "I couldn't find a decision in `carve/decisions.md` about retention for Stripe."
6. **Common patterns** — "where do we calculate X?" → grep models + cite dbt_model entities; "why did we do X?" → search decisions + cite decision entries; "what's the freshness of Y?" → catalog query the destination + cite warehouse_table

The prompt is tuned in iterations during `/build-spec` based on test fixtures.

### CLI

```
carve ask "<question>" [OPTIONS]
  --pipeline TEXT          Scope to one pipeline
  --target TEXT            Scope to one target
  --output [text|json]     Default: text (markdown rendered for terminal); json for piping
  --watch                  Show progress (skill calls as they happen); on by default in TTY

carve asks list [OPTIONS]
  --since DURATION         e.g., 24h, 7d (default: 7d)
  --pipeline TEXT
  --limit INTEGER          Default 50

carve asks show <ask_id> [OPTIONS]
  --output [text|json]
  --include-trace          Print the full skill-call trace
```

`carve ask` is the headline command. `carve asks list` and `carve asks show` are operational verbs for reviewing prior asks.

### REST

```
POST /api/v1/asks                     # body: { question, pipeline?, target? }
                                      # returns the completed Ask synchronously (5-30s typical)
GET  /api/v1/asks/{id}                # show a specific ask
GET  /api/v1/asks                     # list with pagination + filters (?pipeline=, ?since=)
```

Per spec 09's parity rule, every CLI option maps to a REST parameter.

### MCP

Auto-generated by spec 10 from the REST endpoints. Tool naming overrides:

- `POST /api/v1/asks` → tool name **`ask`** (not `asks_create`, because "ask" is the natural verb)
- `GET /api/v1/asks/{id}` → `ask_show`
- `GET /api/v1/asks` → `asks_list`

These overrides go in the `tool_generator.py` override map from spec 10.

### Concurrency model

- Asks do NOT go through the jobs table or the runtime's worker pool
- Asks are served synchronously by the API request handler (or the CLI's local execution for one-shot invocations without `carve serve`)
- An ask invocation holds a DB session for the duration but creates no rows in `jobs`, `runs`, or `step_runs`
- Asks DO write rows in `asks`, `agent_invocations`, and `skill_calls` (so the agent telemetry surface still records them)
- Multiple concurrent asks are fine; the only contention is DB connections, which is well-sized for the v0.1 default of one worker + a few API request slots

### What asks read

Per spec 06's MemorySelector with `is_investigative=True`:

- Always: `carve/conventions.md`, `carve/standards.md`, `carve/decisions.md`
- When `--pipeline X` provided or the question mentions a specific pipeline: `pipelines/X.md`, `pipelines/X.toml`
- When the question mentions an EL artifact: `el/<name>/NOTES.md`
- Via skill calls: anything the orchestrator decides to look up — catalog queries, manifest queries, grep across model files, lineage traversal

### Cited entities — example

Question: *"Why did we pick 18-month retention for Stripe?"*

Answer (markdown):

> We decided to keep Stripe data for 18 months (rather than 24) for storage-cost reasons. The decision is recorded in `carve/decisions.md` and was reviewed by alice@ and bob@.[^1]
>
> The retention is enforced in `dbt_models/staging/stg_stripe_charges.sql` via a `WHERE created_at >= DATEADD(month, -18, current_date())` clause.[^2]
>
> [^1]: carve/decisions.md — 2026-04-12 Stripe retention policy
> [^2]: dbt model — stg_stripe_charges

`cited_entities`:

```json
[
  {"kind": "decision", "identifier": "2026-04-12 Stripe retention policy",
   "excerpt": "Keep Stripe charges in raw_stripe for 18 months, not 24. Storage cost vs analytics utility tradeoff...",
   "source_url": "file://carve/decisions.md#2026-04-12-stripe-retention-policy"},
  {"kind": "dbt_model", "identifier": "stg_stripe_charges",
   "excerpt": "WHERE created_at >= DATEADD(month, -18, current_date())",
   "source_url": "file://dbt_models/staging/stg_stripe_charges.sql"}
]
```

The static UI (spec 11) renders citations as expandable cards with the excerpts. The cloud UI eventually deep-links into file viewers.

### `--watch` mode

In a TTY, the CLI streams the orchestrator's progress while the ask runs:

```
$ carve ask "where do we calculate net revenue?"
  ⌛ Searching dbt models for "net revenue"...
  ✓ Found 3 matches: fct_revenue.sql, stg_stripe_charges.sql, mrt_finance.sql
  ⌛ Reading carve/decisions.md...
  ✓ Found 1 matching decision (2026-03-22 Revenue definition)
  ⌛ Composing answer...

Net revenue is calculated in `fct_revenue.sql` as the sum of charge amounts
...
```

This is implemented by subscribing to the agent's skill-call events and printing a friendly progress line per skill_call. The events feed the same observability surface as plan/build, so the cloud UI also gets live progress.

## Tests

- **Unit (no-write-skills guardrail):** representative write-skill names trigger rejection; read-skill names pass through; MCP tools with `effects.writes=true` are rejected; ambiguous MCP tools are allowed with a warning in the trace
- **Unit (citation builder):** Markdown footnotes are parsed into structured `CitedEntity` objects; missing references produce a warning, not a crash
- **Integration (end-to-end):** ask "where do we calculate net revenue?" against a fixture project with dbt + dlt + decisions → answer cites the right model and decision
- **Integration (no side effects):** before/after asking 10 questions, the state store has +10 ask rows, +N agent_invocations, +N skill_calls, but zero changes to files, zero rows in jobs/runs/builds, and zero changes to the destination warehouse
- **Integration (concurrent):** while a long-running plan is in progress, fire 5 concurrent asks; all complete successfully without blocking on the plan
- **Integration (cites decisions):** ask "why did we pick 18-month retention for Stripe?" with `decisions.md` containing the rationale → answer cites the entry verbatim (excerpt match)
- **Integration (guardrail enforcement):** craft a fixture LLM response that tries to call a write skill mid-ask; verify the guardrail rejects, the ask doesn't fail (LLM picks a different approach), and the trace records the rejection
- **Integration (CLI/REST/MCP parity):** the parity tests from specs 09 and 10 include `ask`, `ask_show`, `asks_list` and confirm they're reachable from all three surfaces

## Acceptance

- `carve ask "<question>"` returns a useful answer within 5–15 seconds for typical questions
- Answers cite the right entities (decisions, dbt models, dlt pipelines, pipeline TOMLs, memory files) when applicable
- The `NoWriteSkillsGuardrail` structurally prevents any file or destination mutation during an ask
- Asks run concurrently with each other and with plan/build/run/deploy without contention
- The state store records ask metadata; the full structured answer + trace is in `.carve/asks/<id>.json`
- The CLI, REST, and MCP surfaces all expose ask functionality per parity rules
- The static UI surfaces recent asks on the index page
- The orchestrator's ask prompt is tested against representative questions during `/build-spec` iteration

## Design notes

- **Why reuse the orchestration agent rather than ship a dedicated "ask" agent?** Because the ask path is mostly identical to plan: classify intent, gather context, reason about the answer. The differences are output shape and the write-skill restriction — both cleanly handled by configuration + a guardrail. Shipping a separate agent would duplicate prompt content, skill registration, and reasoning machinery.
- **Why is the answer synchronous (no streaming in v0.1)?** Because the typical ask takes 5–15 seconds, which is fine for synchronous return. Streaming the answer would require a different protocol (SSE/WebSocket on the asks endpoint) and a different client-side UX. The `--watch` mode streams *progress* (which skill is running) which is the more useful UX for a 10-second wait.
- **Why is `decisions.md` included by default in ask but not in plan?** Because asks frequently look backward ("why did we…?") whereas plans look forward. Loading decisions into plan context wastes tokens for the typical case where the plan doesn't reference past decisions. The selector's `is_investigative` flag is the explicit toggle.
- **Why no separate jobs/queue for asks?** Because asks are short, synchronous, and stateless from the runtime's perspective. Adding them to the jobs table would create contention with scheduled runs for no benefit; the API request handler does the work directly.
- **Why a separate citations data structure rather than embedding citations in the markdown?** Because consumers (static UI, cloud UI, external agents via MCP) want structured citations they can render as cards / deep-links. Markdown alone forces every consumer to parse footnotes themselves. The duplication (markdown footnotes + structured cited_entities) is intentional: markdown for human readability, structured data for programmatic consumption.
- **Why does the `NoWriteSkillsGuardrail` use both name patterns and effect annotations rather than a strict allowlist of read skills?** Because new read skills get added all the time (every MCP server the user installs adds tools). An allowlist would block legitimate read skills until updated. A blocklist (write-skill patterns + effects annotation) errs in the right direction — false positives block writes (correct), false negatives are rare and detectable via the trace (auditable).

## Open questions

- **Whether asks should cite warehouse rows (e.g., "Stripe had 12,438 charges loaded yesterday").** *Implementation default.* The agent can call `destination_schema_query` during an ask; that's a read skill, allowed. Whether to add a dedicated "sample some rows" skill that returns row excerpts as citation_entity excerpts. Defer to post-v0.1 — users who need row-level data via ask can compose a SQL step in their pipelines and ask about the output.
- **Maximum ask invocation duration.** *Implementation default.* 60-second hard cap; longer asks are likely going in circles and consuming tokens. Cap is enforced by the orchestrator's `max_iterations` from the agent definition (20 iterations × typical 3s per iteration = ~60s).
- **Whether `carve ask` should suggest a follow-up `carve plan` when the answer reveals a change is needed.** *Implementation default.* The prompt steers toward "here's what I found; if you want to change it, run `carve plan "<suggested phrasing>"`." The CLI surface doesn't take action automatically — the user picks.
- **Asks-as-citations: when an ask is itself useful context for a later plan/ask.** *Implementation default.* Not in v0.1; each ask is independent. A future enhancement could index ask answers and let agents discover them ("we've answered this before"). Defer.
