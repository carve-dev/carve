# `carve ask`: the explorer (read-only mode of the harness)

> **Revised for the AI-harness model** (see [../_strategy/2026-06-ai-harness.md](../_strategy/2026-06-ai-harness.md)): `ask` is the **explorer** — a declarative **subagent** (`builtin/explorer.md`) the orchestrator `delegate`s read-only questions to, run in the **`read_only` permission mode** (spec 15) with read-only **terminal-grade tools** (`read_file`/`grep`/`glob`/`web_fetch`) + the dialect-aware **`sql` tool on the read role** (spec 18) + lineage/manifest/memory skills. The former `NoWriteSkillsGuardrail` is **subsumed by the `read_only` mode**: the permission gate (not a separate ask-only mechanism) structurally enforces no-writes. The citation model (`cited_entities`), the Ask data model + persistence, investigative memory selection, and the CLI/REST/MCP surface are unchanged.

> The fifth lifecycle verb (sibling to plan/build/run/deploy), strictly side-effect-free. `carve ask` invokes the explorer subagent in `read_only` mode and returns a markdown answer with cited entities instead of a Plan. Per [PRD §6.5 ask](../PRD.md), [PRD §3 core loop sibling-verb note](../PRD.md), [ARCHITECTURE §7.1 ask](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 12](../PROJECT_PLAN.md).

## Status

- **Status:** Drafting
- **Depends on:** [harness](./harness.md) (the explorer is a subagent run in the `read_only` permission mode, with the terminal tools + the `delegate` path), [extensibility](./extensibility.md) (the explorer ships as a declarative agent — `builtin/explorer.md`), [sql](./sql.md) (the explorer queries the warehouse via the `sql` tool on the **read role**), [state-store](./state-store.md), [memory](./memory.md) (asks cite `decisions.md` entries), [rest-api](./rest-api.md) (this spec adds the asks router to the existing FastAPI app)
- **Soft depends on:** M1's reasoning loop (HISTORICAL, extended into the harness by spec 15); spec 07/08 for run/pipeline data the explorer reads when answering
- **Blocks:** nothing structurally

## Goal

Add the `ask` verb as **the explorer** — the read-only mode of the harness. Concretely:

1. **The explorer is a declarative subagent** (`builtin/explorer.md`, spec 16) run in the **`read_only` permission mode** (spec 15). It is the orchestrator's `delegate` target for read-only how/where/why questions, and `carve ask` invokes it directly in `read_only` mode. No write tools, ever — the permission gate enforces this structurally.
2. **Ask data model**: `asks` table for indexing + `.carve/asks/<id>.json` for durability
3. **Answer format**: markdown text + cited entities (structured references into lineage / decisions / pipeline files) + the full tool-call trace
4. **CLI**: `carve ask "<question>"`, `carve asks list`, `carve asks show <id>`
5. **REST router** added to the spec-09 FastAPI app: `POST /api/v1/asks`, `GET /api/v1/asks/{id}`, `GET /api/v1/asks`
6. **MCP tool** auto-generated per spec 10 (tool name `ask`)
7. **Concurrency**: asks run in parallel with each other and with plan/build/run/deploy. No queue lock; no rows in the jobs table.

After this spec lands, a user can ask "where do we calculate net revenue?" or "why did we pick 18-month retention for Stripe?" and get a cited answer in 5–15 seconds — without anything in the project changing.

## Out of scope

- **The harness core** — the subagent loop, the `delegate` tool, the terminal tools, the `read_only` permission gate, and role-scoped warehouse access all live in [harness](./harness.md); the declarative agent format in [extensibility](./extensibility.md); the `sql` tool in [sql](./sql.md). This spec **consumes** them and ships the explorer's agent definition, its system prompt, the Ask data model, and the citation builder.
- Streaming the answer token-by-token (synchronous return in v0.1; the answer arrives as one chunk when the explorer finishes)
- Asking *about* asks (e.g., "what have we been asking lately?") — handled by `carve asks list` rather than a meta-ask
- Embedding-based semantic search over decisions/code (post-v0.1; v0.1 explorer uses the read tools + skills directly)
- Conversational follow-ups (each ask is a single round; multi-turn chat lives in the chat tool, e.g., Claude Desktop, which can call `ask` repeatedly)

## Files this spec produces

```
src/carve/core/agents/builtin/explorer.md               # NEW — the explorer declarative agent (frontmatter: read-only tools + read_only mode; system-prompt body); loaded by spec 16's AgentRegistry
src/carve/core/agents/run_explorer.py                   # NEW — invoke the explorer subagent in read_only mode (via spec 15's SubagentRunner / delegate) and post-process its result into an Answer

src/carve/core/asks/__init__.py
src/carve/core/asks/store.py                            # NEW — persistence (.carve/asks/<id>.json + asks table index)
src/carve/core/asks/models.py                           # NEW — Ask, Answer, CitedEntity dataclasses
src/carve/core/asks/citation_builder.py                 # NEW — converts the explorer's cited references into structured CitedEntity objects

src/carve/cli/ask.py                                    # NEW — `carve ask`, `carve asks list`, `carve asks show` commands

src/carve/api/routers/asks.py                           # NEW — wires asks endpoints into the spec-09 app

src/carve/core/state/models.py                          # MODIFY — add Ask table
migrations/versions/0011_asks.py                        # NEW

tests/unit/test_explorer_read_only_mode.py              # NEW — the explorer's tool grant resolves to the read tools only; the read_only permission gate denies edit/bash-write/warehouse-write (no separate ask-only guardrail)
tests/unit/test_ask_citation_builder.py                 # NEW
tests/integration/test_ask_end_to_end.py                # NEW — real ask against a fixture project; verify answer + citations
tests/integration/test_ask_no_side_effects.py           # NEW — files / state store / destination unchanged after ask
tests/integration/test_ask_concurrent_with_plan.py      # NEW — ask runs alongside plan/build/run without contention
tests/integration/test_ask_cites_decisions.py           # NEW — "why did we do X?" surfaces decisions.md entries

docs/ask.md                                             # NEW — what the explorer does, example questions, how it differs from plan
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
        "tool_call",               # a specific tool/skill-call row (sql, grep, dbt_manifest, dlt_schema, …) used to produce this part of the answer
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
    tool_call_trace_path: Path                # .carve/asks/<id>.json — the explorer's full tool-call trace
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
  tool_call_trace_path TEXT NOT NULL,      -- relative path under .carve/asks/ (the explorer's tool-call trace)
  tenant_id BIGINT NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX ix_asks_created_at ON asks(tenant_id, created_at DESC);
CREATE INDEX ix_asks_pipeline ON asks(pipeline) WHERE pipeline IS NOT NULL;
```

The full structured answer + tool-call trace is persisted to `.carve/asks/<id>.json` for durability and offline-readability. The DB row is the index.

### The explorer subagent

`src/carve/core/agents/run_explorer.py` invokes the **explorer** subagent in the `read_only` permission mode and post-processes its result into an `Answer`:

```python
def run_ask(   # sync; the async carve serve / REST layer invokes it in a threadpool (spec 15)
    *,
    question: str,
    pipeline: Optional[str],
    target: Optional[str],
    delegate: DelegateTool,               # spec 15's delegation entrypoint (builds a fresh SubagentRunner)
    memory_loader: MemoryLoader,
    state_store: StateStore,
) -> Ask:
    ask_id = uuid4()
    ask = Ask(id=ask_id, question=question, pipeline=pipeline, target=target, status="pending", ...)
    await state_store.asks.create(ask)
    try:
        # The context bundle the explorer subagent starts from (spec 15). The explorer then
        # gathers what else it needs within its own context window via its read tools/skills.
        context = compose_ask_context(
            question=question,
            pipeline=pipeline,
            target=target,
            memory_loader=memory_loader,
            is_investigative=True,         # ensures decisions.md is included (spec 06's selector)
        )
        # delegate to the explorer; the harness runs it in read_only mode (set from the agent's
        # frontmatter + the ask verb) and returns a DelegationResult summary, not the transcript.
        result = delegate(   # sync (spec 15); run_explorer is invoked from the async serve via a threadpool
            agent="explorer",
            task=question,
            context=context,
        )
        answer = build_answer_from_result(result)   # parse markdown footnotes -> cited_entities
        return await state_store.asks.complete_succeeded(ask_id, answer, result.usage)
    except SubagentError as e:
        return await state_store.asks.complete_failed(ask_id, str(e))
```

Key behaviors:

- **The explorer is its own subagent**, not the orchestrator with a guardrail. `carve ask` (and the orchestrator's `delegate` for read-only questions) runs the `explorer` agent in the **`read_only` permission mode** (spec 15). The mode — not an ask-only mechanism — structurally forbids writes.
- **Read tools + skills only** (`read_file`/`grep`/`glob`/`web_fetch`, the `sql` tool on the read role, the `dbt_manifest`/`dlt_schema`/`memory_read` skills). There are **no write tools in the grant**, and the permission gate denies any write attempt regardless.
- **Self-gathers context.** Rather than the parent pre-scoping everything, the explorer is handed a small context bundle and reads what it needs within its own isolated window (spec 15 supersedes the manual pre-scoped-context pattern). It does not delegate further — it produces the answer itself.
- **Output post-processing** turns the explorer's `DelegationResult` (markdown + footnotes) into the `Answer` (markdown + structured `cited_entities`) via `citation_builder.py`.

### No-writes via the `read_only` permission mode (subsumes the old guardrail)

In the M1-era design this was a bespoke `NoWriteSkillsGuardrail`. Under the harness, **the `read_only` permission mode is the enforcement** (spec 15) — there is no separate ask-only mechanism. Two layers make writes structurally impossible:

1. **Tool grant.** The explorer's frontmatter grants only read tools (`read_file`, `grep`, `glob`, `web_fetch`, `sql`, and read-only skills). `edit`, `bash` (in any write capacity), the `dlt_library` copy, and resource-mutating REST/MCP tools are simply **not in the grant**.
2. **Pre-execution gate.** Even a granted tool is checked by the `read_only` policy before it runs (spec 15's `permissions.py`): `read_only` permits `read_file`/`grep`/`glob`/`web_*`/**read-only SQL only**; no `edit`, no writing `bash`, no warehouse writes. The `sql` tool runs on the **read role** (spec 18), so a `SELECT` is allowed but any `INSERT`/`DDL`/load is denied at the role *and* the mode. MCP tools carry `effects` metadata (spec 16); a `writes=true` MCP tool is denied in `read_only` (an MCP tool that doesn't declare effects is treated conservatively per spec 16).

A blocked tool call returns a tool_error to the model ("`read_only` mode: `<tool>` performs writes and was blocked") so it can pick a different approach. If the explorer cannot make progress read-only (repeated denials — more than 3 in one invocation), the ask completes as `failed` with "this question cannot be answered without write access; try `carve plan`." The exact thresholds/messages are the harness's (spec 15); this spec only relies on the guarantee that **no file, git, or warehouse write can occur during an ask.**

### The explorer agent file: `builtin/explorer.md`

A built-in **declarative agent** (spec 16 format), shipped at `src/carve/core/agents/builtin/explorer.md` and overridable by a user `carve/agents/explorer.md`. Frontmatter pins the read-only tool grant and the classifications the orchestrator routes on; the body is the system prompt.

```markdown
---
name: explorer
description: Read-only Q&A about the project — how/where/why, lineage, logic, definitions, tests, "where does this data come from." Use for investigative questions that change nothing.
model: claude-{LATEST_SONNET}   # per-agent tiering (spec 16); falls back to the install default
tools: [read_file, grep, glob, web_fetch, sql, dbt_manifest, dlt_schema, memory_read]   # read-only; no edit/bash-write. Lineage is investigated (dbt_manifest + dlt_schema + grep), not a Carve graph — spec 19.
allowed_paths: []               # writes nothing
classifications: [explain, locate, lineage, why_decision, freshness, test_coverage]
---
<system prompt body…>
```

The active **permission mode is `read_only`** (set by the `ask` verb / the orchestrator's `delegate`, spec 15) — the grant above is further gated by that mode, and the `sql` tool resolves to the **read role** (spec 18).

The system-prompt body has the following structure:

1. **Role** — "You answer the user's question about their project using only read tools. You change nothing."
2. **Inputs** — what's in the context bundle (memory, project state, target) and that you gather the rest yourself with your read tools.
3. **Tools** — the read tools/skills available (`read_file`/`grep`/`glob`/`web_fetch`, the `sql` tool, the `dbt_manifest`/`dlt_schema`/`memory_read` skills); an explicit reminder that you are in `read_only` mode so any write is blocked.
4. **Answer format** — markdown body + a list of citations (one per claim with non-obvious provenance). Citations are inline as Markdown footnotes `[^1]` with the entity references in a structured tail block the citation_builder parses.
5. **Style** — concise, factual, no apologies for not being able to do things. If the project doesn't have the information, say so directly: "I couldn't find a decision in `carve/decisions.md` about retention for Stripe."
6. **Common patterns** — "where do we calculate X?" → `grep` models + cite dbt_model entities; "why did we do X?" → search decisions + cite decision entries; "what's the freshness of Y?" → `sql` introspect the destination (read role) + cite warehouse_table.

The prompt is tuned in iterations during `/build-spec` based on test fixtures.

### CLI

```
carve ask "<question>" [OPTIONS]
  --pipeline TEXT          Scope to one pipeline
  --target TEXT            Scope to one target
  --output [text|json]     Default: text (markdown rendered for terminal); json for piping
  --watch                  Show progress (the explorer's tool calls as they happen); on by default in TTY

carve asks list [OPTIONS]
  --since DURATION         e.g., 24h, 7d (default: 7d)
  --pipeline TEXT
  --limit INTEGER          Default 50

carve asks show <ask_id> [OPTIONS]
  --output [text|json]
  --include-trace          Print the full tool-call trace
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
- Asks are served synchronously by the API request handler (or the CLI's local execution for one-shot invocations without `carve serve`); each runs the explorer subagent in `read_only` mode
- An ask invocation holds a DB session for the duration but creates no rows in `jobs`, `runs`, or `step_runs`
- Asks DO write rows in `asks`, `agent_invocations`, and the harness's tool-call telemetry (so the agent observability surface still records them; the explorer's cost/tokens roll up to its invocation per spec 15)
- Multiple concurrent asks are fine; the only contention is DB connections, which is well-sized for the v0.1 default of one worker + a few API request slots

### What asks read

Per spec 06's MemorySelector with `is_investigative=True`:

- Always: `carve/conventions.md`, `carve/standards.md`, `carve/decisions.md`
- When `--pipeline X` provided or the question mentions a specific pipeline: `pipelines/X.md`, `pipelines/X.toml`
- When the question mentions an EL artifact: `el/<name>/NOTES.md`
- Via its read tools/skills: anything the explorer decides to look up within its own context window — `sql` introspection/reads (read role), dbt-manifest queries, `dlt_schema` (dlt's resource→table), `grep`/`glob` across model files, lineage investigation (correlating these in-context), `web_fetch` of docs

(Memory selection seeds the explorer's context bundle; the explorer then self-gathers the rest, per spec 15's context-isolation model.)

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

In a TTY, the CLI streams the explorer's progress while the ask runs:

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

This is implemented by subscribing to the explorer's tool-call events and printing a friendly progress line per tool call. The events feed the same observability surface as plan/build, so the cloud UI also gets live progress.

## Tests

- **Unit (explorer in `read_only` mode):** the explorer's tool grant resolves to read tools only; under `read_only` the permission gate denies `edit`, write-`bash`, and warehouse writes (the `sql` tool runs on the read role); an MCP tool with `effects.writes=true` is denied. (Mode enforcement is spec 15's; this asserts the explorer is wired to it — no separate ask-only guardrail.)
- **Unit (citation builder):** Markdown footnotes are parsed into structured `CitedEntity` objects; missing references produce a warning, not a crash
- **Integration (end-to-end):** ask "where do we calculate net revenue?" against a fixture project with dbt + dlt + decisions → answer cites the right model and decision
- **Integration (no side effects):** before/after asking 10 questions, the state store has +10 ask rows, +N agent_invocations, +N tool-call rows, but zero changes to files, zero rows in jobs/runs/builds, and zero changes to the destination warehouse
- **Integration (concurrent):** while a long-running plan is in progress, fire 5 concurrent asks; all complete successfully without blocking on the plan
- **Integration (cites decisions):** ask "why did we pick 18-month retention for Stripe?" with `decisions.md` containing the rationale → answer cites the entry verbatim (excerpt match)
- **Integration (write attempt blocked):** craft a fixture LLM response that tries to `edit` a file (or run a write via `bash`/`sql`) mid-ask; verify the `read_only` gate blocks it, the ask doesn't fail (the model picks a different approach), and the trace records the denial
- **Integration (CLI/REST/MCP parity):** the parity tests from specs 09 and 10 include `ask`, `ask_show`, `asks_list` and confirm they're reachable from all three surfaces

## Acceptance

- `carve ask "<question>"` runs the explorer in `read_only` mode and returns a useful answer within 5–15 seconds for typical questions
- Answers cite the right entities (decisions, dbt models, dlt pipelines, pipeline TOMLs, memory files) when applicable
- The `read_only` permission mode (not a bespoke guardrail) structurally prevents any file, git, or warehouse mutation during an ask
- Asks run concurrently with each other and with plan/build/run/deploy without contention
- The state store records ask metadata; the full structured answer + trace is in `.carve/asks/<id>.json`
- The CLI, REST, and MCP surfaces all expose ask functionality per parity rules
- The static UI surfaces recent asks on the index page
- The explorer agent (`builtin/explorer.md`) is tested against representative questions during `/build-spec` iteration

## Design notes

- **Why a dedicated explorer subagent (vs. the M1 plan-orchestrator-with-a-guardrail)?** Under the harness, "ask" is just the **`read_only` mode** of a subagent that gathers context and answers — exactly the Claude-Code explorer pattern. A declarative `builtin/explorer.md` with a read-only tool grant is simpler and *safer* than bolting a write-blocking guardrail onto the orchestrator: the mode enforces no-writes for the explorer and every other read-only delegate uniformly, the agent is user-overridable like any other, and context-isolation (spec 15) keeps an investigation's tool churn out of the orchestrator's window. It does **not** delegate further — it answers itself.
- **Why is the answer synchronous (no streaming in v0.1)?** Because the typical ask takes 5–15 seconds, which is fine for synchronous return. Streaming the answer would require a different protocol (SSE/WebSocket on the asks endpoint) and a different client-side UX. The `--watch` mode streams *progress* (which tool is running) which is the more useful UX for a 10-second wait.
- **Why is `decisions.md` included by default in ask but not in plan?** Because asks frequently look backward ("why did we…?") whereas plans look forward. Loading decisions into plan context wastes tokens for the typical case where the plan doesn't reference past decisions. The selector's `is_investigative` flag is the explicit toggle.
- **Why no separate jobs/queue for asks?** Because asks are short, synchronous, and stateless from the runtime's perspective. Adding them to the jobs table would create contention with scheduled runs for no benefit; the API request handler does the work directly.
- **Why a separate citations data structure rather than embedding citations in the markdown?** Because consumers (static UI, cloud UI, external agents via MCP) want structured citations they can render as cards / deep-links. Markdown alone forces every consumer to parse footnotes themselves. The duplication (markdown footnotes + structured cited_entities) is intentional: markdown for human readability, structured data for programmatic consumption.
- **Why enforce no-writes via the `read_only` permission mode rather than a bespoke ask-only guardrail?** Because the harness already needs a permission gate for *every* agent that runs `bash` + touches the warehouse + pushes git (spec 15). Reusing `read_only` for the explorer means one trust mechanism, not two: the mode's tool/path/role policy is the single place writes are denied, it composes with MCP `effects` metadata (spec 16) so newly-installed tools are classified automatically, and it errs correctly — a write tool is denied even if it slipped into the grant. A separate guardrail would duplicate and could drift from the mode it shadows.

## Open questions

- **Whether asks should cite warehouse rows (e.g., "Stripe had 12,438 charges loaded yesterday").** *Implementation default.* The explorer can `sql` introspect/`SELECT` on the read role during an ask (allowed in `read_only`). Whether to add a dedicated "sample some rows" affordance that returns row excerpts as `cited_entity` excerpts. Defer to post-v0.1 — users who need row-level data via ask can compose a `sql` step in their pipelines and ask about the output.
- **Maximum ask invocation duration.** *Implementation default.* 60-second hard cap; longer asks are likely going in circles and consuming tokens. The cap is enforced by the explorer subagent's `max_turns` (spec 15), tuned so ~20 turns × typical 3s ≈ 60s.
- **Whether `carve ask` should suggest a follow-up `carve plan` when the answer reveals a change is needed.** *Implementation default.* The prompt steers toward "here's what I found; if you want to change it, run `carve plan "<suggested phrasing>"`." The CLI surface doesn't take action automatically — the user picks.
- **Asks-as-citations: when an ask is itself useful context for a later plan/ask.** *Implementation default.* Not in v0.1; each ask is independent. A future enhancement could index ask answers and let agents discover them ("we've answered this before"). Defer.
