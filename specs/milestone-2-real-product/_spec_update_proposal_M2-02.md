# M2-02 — Orchestration agent

**Milestone:** 2 — Real product
**Estimated effort:** 1.5 days
**Dependencies:** M1-04 (agent loop), M1.1-06 (plan/build/run separation; m1_plan_agent), M2-01 (plan schema), M2-09 (schema retrieval skills)

## Update notes (proposal)

M1.1-06 already shipped the first piece of what M2 generalizes:

- A single-purpose **plan agent** lives at `src/carve/core/agents/prompts/m1_plan_agent.md`. Its tools are `read_file`, `run_snowflake_query`, and `submit_plan(design)`; it terminates the agent loop via `AgentLoop.terminator_tool="submit_plan"` (`src/carve/core/agents/loop.py`, `src/carve/core/agents/m1_tools.py::make_submit_plan_tool`).
- That agent emits a **single-pipeline design** — destination, source, transformation, columns, tradeoffs — persisted as a `Plan` row with `phase="drafted"`, `pipeline_name`, `parent_plan_id`. The build agent later consumes the design.
- `carve plan`, `carve plan --refine`, and `carve plan --pipeline <name>` are already wired through `src/carve/cli/orchestrator/planner.py`.

M2-02 is the **next layer up**: the orchestration agent decides whether a goal is a single-pipeline ingest (delegate to `m1_plan_agent` for the design, wrap as a one-task plan with `agent="extract_load"`) or a multi-agent goal that needs a real task graph spanning extract-load / dbt / snowflake. The orchestrator does *not* re-implement what the plan agent already does; it routes to it.

This proposal:

1. Renames the relationship: M2-02 is an orchestrator that **delegates to the existing m1_plan_agent for single-pipeline ingest design** and otherwise builds a multi-task plan itself.
2. Adds the delegation path to the deterministic-shortcuts table.
3. References M2-01 for the `Plan` schema instead of restating it.
4. Names the new build-time **extract-load specialist** (M2-03 spec) as the build-time owner of Python extract-and-load scripts. The orchestrator routes ingest tasks to it; the build coordinator (M2-01) dispatches to it at build time.
5. Updates the `Files` section to reflect that we're building on top of `src/carve/core/agents/`, not greenfield.
6. Leaves goal classification, two-layer agent selection, impact analysis, pre-scoping, estimates, investigation handling, and manual override unchanged.

## Purpose

The orchestration agent is the only agent that knows about other agents. It takes a goal, classifies it, gathers impact context, picks the right specialist agents, and produces a `Plan` (schema defined in M2-01) with a task graph.

For single-pipeline ingest goals — the M1.1 happy path — the orchestrator does not fan out: it delegates to the existing M1.1 plan agent (`m1_plan_agent`) and wraps the resulting design as a one-task plan in the M2-01 schema. For everything else (modifying dbt models, refactoring marts, mixed pipeline+dbt goals, investigation), the orchestrator generates a multi-task graph itself.

This is the most important new agent in M2 because it determines how every other agent gets invoked.

## Relationship to the M1.1 plan agent

The M1.1-06 plan agent (`src/carve/core/agents/prompts/m1_plan_agent.md`) stays. M2-02 sits in front of it, not in place of it.

Decision: **M2-02 delegates to `m1_plan_agent` for the design phase of single-pipeline ingest goals; supersedes it for multi-agent goals.**

Concretely:

- For a goal classified as single-pipeline ingest (today's `carve plan "<goal>"` flow), the orchestrator delegates to `m1_plan_agent` to produce a single-pipeline design (source/destination/transformation/columns/tradeoffs). The orchestrator wraps that design as the `inputs` of a one-task `Plan` whose `Task` has `agent="extract_load"`. At build time, the build coordinator (M2-01) invokes the **extract-load agent** (M2-03) to write the Python code from that design.
- For multi-agent goals (modifications spanning dbt+snowflake, mart refactors, quality additions, etc.), the orchestrator builds a multi-task graph using its layer-2 selection. Each task targets a build-time specialist (`extract_load`, `dbt`, `snowflake`, `quality`).
- For investigation goals, the orchestrator answers directly (no task graph).

This keeps M1.1's terminator-tool flow intact (the plan agent still runs to a single `submit_plan` and terminates) and avoids reworking the plan path. The build path *does* change (per M2-01): the M1.1 build agent becomes a coordinator that dispatches to specialists rather than authoring code itself.

### Plan-time vs build-time agent roles

This is the cleanest mental model:

| Phase | Agent | Role | Lives in |
|---|---|---|---|
| Plan time | **Orchestrator** (M2-02) | Classifies goal, picks specialists, produces task graph as a `Plan` | `src/carve/core/agents/orchestration/` |
| Plan time | **`m1_plan_agent`** (M1.1-06) | Plan-time helper for single-pipeline ingest goals; produces the design that becomes a Task's `inputs` | `src/carve/core/agents/prompts/m1_plan_agent.md` |
| Build time | **Build coordinator** (M2-01) | Reads task graph, dispatches each task to its specialist, verifies + stitches | `src/carve/cli/orchestrator/builder.py` + `prompts/build_coordinator.md` |
| Build time | **Extract-load agent** (M2-03) | Specialist: writes Python extract-load scripts | `src/carve/core/agents/extract_load.py` + `prompts/extract_load_agent.md` |
| Build time | **dbt agent** (M2-04) | Specialist: writes dbt models, schema.yml, docs | `src/carve/core/agents/dbt.py` + `prompts/dbt_agent.md` |
| Build time | **Snowflake agent** (M2-05) | Specialist: writes DDL/RBAC | `src/carve/core/agents/snowflake.py` + `prompts/snowflake_agent.md` |

The orchestrator and `m1_plan_agent` produce a *plan*. The build coordinator dispatches that plan's tasks to *build-time specialists* who write the code. These are distinct agents at distinct lifecycle phases — don't conflate them.

## Responsibilities

1. **Goal classification** — is this a new build, modification, refactor, or investigation?
2. **Impact analysis** — what existing artifacts does this goal touch?
3. **Agent selection** — which specialist agents are needed; which are skipped
4. **Task graph generation** — order, dependencies, expected outputs (using M2-01's `Plan` schema)
5. **Cost and duration estimation**
6. **Skipped-agent transparency** — record why each unselected agent was excluded
7. **Delegation to `m1_plan_agent`** — for single-pipeline ingest goals, produce a one-task plan that invokes the existing M1.1 plan agent rather than duplicating its work

## Two-layer agent selection

### Layer 1 — deterministic shortcuts

Cheap pattern matching on the goal text. Implemented in `src/carve/core/agents/orchestration/shortcuts.py`:

| Pattern | Inferred routing |
|---|---|
| Single-pipeline ingest goal (verbs like "ingest", "load", "pull from", references a single source URL/system, no existing-mart references) → | delegate to `m1_plan_agent` for design; one-task plan with `agent="extract_load"` |
| Mentions only existing dbt model names → | dbt agent only |
| Mentions a new source system not in any pipeline (and dbt context implied) → | extract_load + snowflake (+ dbt if marts implied) |
| Read-only verbs ("explain", "show", "why") → | no write agents; investigation only |
| Mentions schema/role/warehouse → | include snowflake |
| Mentions tests, freshness, anomalies → | include quality |

Shortcuts produce a *suggestion* that goes into the layer-2 prompt; they don't bind the decision — except the single-pipeline-ingest shortcut, which short-circuits to `m1_plan_agent` without a layer-2 call when confidence is high (preserves M1.1's cheap path).

### Layer 2 — LLM-based selection

A focused LLM call (Sonnet, not Opus — this is routing, not synthesis):

**System prompt:**
```
You are the routing component of a multi-agent system for data engineering.

Given a user goal, decide:
1. Which specialist agents to invoke (and which to skip)
2. The order they should run
3. A brief description of what each agent should do

Available build-time specialist agents:
- extract_load: author Python extract-and-load scripts (M2-04; the build-time
  specialist that writes pipelines/<name>/main.py from a design that
  m1_plan_agent produced at plan time)
- dbt: generate, modify, or refactor dbt models, tests, and documentation
- snowflake: manage Snowflake DDL, RBAC, warehouses
- quality: generate tests, freshness checks, anomaly detection rules
  (M2: covered by dbt agent; split out in M3)

Only include agents that are clearly needed. Be willing to invoke just one.
For each skipped agent, briefly state why.

Output JSON matching the provided schema.
```

**User message** (assembled by the orchestrator):
```
Goal: <goal text>

Existing project context:
- dbt project at <path>, <N> models, <M> sources
- Active pipelines: <list>
- Snowflake databases in scope: <list>

Impact analysis (from skill calls):
- Affected models: <list>
- Affected sources: <list>
- New objects required: <list or "none">

Shortcut suggestion (may be wrong; use as a hint):
<suggestion>
```

The LLM responds with structured JSON matching:

```python
class AgentSelection(BaseModel):
    invoked: list[InvokedAgent]
    skipped: list[SkippedAgent]

class InvokedAgent(BaseModel):
    name: str
    order: int
    scope: str  # what this agent should do, in 1-2 sentences

class SkippedAgent(BaseModel):
    name: str
    reason: str  # why this agent isn't needed
```

## Goal classification

A separate small LLM call (or a heuristic) categorizes the goal:

```python
class GoalClass(Enum):
    NEW_BUILD = "new_build"      # introducing a new source or mart
    MODIFICATION = "modification" # changing existing
    REFACTOR = "refactor"         # restructuring without behavior change
    INVESTIGATION = "investigation"  # read-only
```

This shapes the prompt sent to layer 2 and determines what impact analysis to run. A `NEW_BUILD` goal that's also single-pipeline-ingest is the canonical delegate-to-`m1_plan_agent` case.

## Impact analysis

For modifications and refactors, run these skills before agent selection:

- `lookup_dbt_model(<name>)` for any model mentioned in the goal
- `get_downstream_dependencies(<model>)` for affected models
- `list_tables(database, schema)` for any schema mentioned

The results become structured context in the layer-2 prompt.

For new builds, less analysis is needed — just check what doesn't already exist. Pipeline-modification goals (`carve plan --pipeline <name>`) include the existing pipeline files in the delegation context, the same way M1.1 does today.

## Plan output

The orchestrator emits a `Plan` matching the schema defined in **M2-01** (`src/carve/core/plan/schema.py`). M2-02 does **not** redefine the schema. Each `Task` in `task_graph` references one specialist agent.

For the single-pipeline-ingest shortcut, the produced plan has exactly one task with `agent="extract_load"`. The orchestrator runs `m1_plan_agent` at plan time to produce the design (source/destination/transformation/columns/tradeoffs), then embeds that design into the task's `inputs` along with the goal, connection context, and any existing pipeline files (for `--pipeline <name>` flows). At build time, the build coordinator dispatches the task to the **extract-load agent** (M2-04), which reads the embedded design and writes `pipelines/<name>/main.py` and `requirements.txt`.

## Task graph generation

After selection, the orchestrator generates the task graph. For M2 this is sequential:

```python
def build_task_graph(selection: AgentSelection, goal: str, context: dict) -> list[Task]:
    tasks = []
    for invoked in selection.invoked:
        tasks.append(Task(
            step=invoked.order,
            agent=invoked.name,
            action=infer_action(goal, invoked.scope),
            inputs={
                "goal": goal,
                "scope": invoked.scope,
                "context": context,  # pre-scoped context for this agent
            },
            expected_outputs=infer_expected_outputs(invoked, context),
        ))
    return sorted(tasks, key=lambda t: t.step)
```

`expected_outputs` lists predicted file changes (which files will be created/modified). These power the plan's `file_diffs` summary. They're predictions — the actual diffs may differ slightly. M3 can refine this with stricter contract enforcement.

For the single-pipeline-ingest path, `expected_outputs` is the predicted `pipelines/<name>/main.py` and `requirements.txt`. The actual `pipeline_name` is finalized when `m1_plan_agent` calls `submit_plan` at plan time (mirroring M1.1's existing behavior); the extract-load agent at build time consumes that name from the task's `inputs.design.pipeline_name`.

## Pre-scoping context for specialists

This is the big architectural lever. Specialist agents receive *focused* context, not the whole project:

```python
context = {
    "convention_doc": load_conventions_md(),
    "affected_models": [
        {"name": "stg_orders", "sql": "...", "schema_yml": "..."},
        # only the models in scope, not all 142
    ],
    "downstream_dependents": [
        {"name": "int_orders_enriched", "depends_on_order_field": True},
    ],
    "snowflake_schema_summary": {...},  # only relevant schemas
}
```

For the single-pipeline-ingest case, pre-scoping reuses M1.1's existing connection-context and pipeline-context preambles when invoking `m1_plan_agent` at plan time — no new mechanism. The resulting design is then embedded as the extract-load task's `inputs`, which carry through to the build-time extract-load agent.

This is what keeps token usage bounded even for large projects.

## Estimates

Estimates come from a few sources combined:

- **Token count estimate**: based on the size of pre-scoped context + per-agent typical output sizes
- **LLM cost**: tokens × model rate (use the M1 pricing table)
- **Duration**: rough heuristic — 30-60s per agent invocation
- **Snowflake credits**: 0 for plan-only; nonzero only when DDL or queries are involved

These are intentionally rough. Users care about order-of-magnitude (cents vs dollars, seconds vs minutes), not precision.

## Handling investigation goals

For `INVESTIGATION` goals (read-only, "explain why X"), the orchestrator may not generate a task graph at all. Instead, it answers directly using its retrieval skills:

```python
if classification == GoalClass.INVESTIGATION:
    answer = self.investigate(goal, context)
    return Plan(
        id=...,
        goal=goal,
        task_graph=[],  # no tasks
        # ... but include the answer somewhere visible
    )
```

The CLI renders investigations as a Q&A response, not a task graph. M3's UI will have a dedicated investigation view.

## Manual override

Power users can constrain agent selection:

- `carve plan "..." --agents dbt,quality` — force these agents
- `carve plan "..." --skip-agents pipeline` — exclude these

Orchestrator respects these as hard constraints, even if its LLM disagrees. Surfaces a warning in the plan if the manual selection conflicts with the LLM's suggestion.

## Tests

- Goal "ingest the Iowa liquor sales feed" routes to a one-task plan with `agent="extract_load"` whose design content was produced by `m1_plan_agent` at plan time
- Goal "make stg_orders incremental" routes to dbt + quality only
- Goal "onboard Salesforce" routes to extract_load + snowflake + dbt + quality
- Goal "explain why mart_revenue is high today" routes to investigation
- Manual `--agents dbt` overrides LLM suggestion
- Investigation goals don't generate task graphs
- Single-pipeline-ingest shortcut short-circuits without a layer-2 LLM call when confidence is high

Use mocked LLM responses for deterministic tests.

## Acceptance criteria

- Orchestration agent produces a `Plan` (M2-01 schema) from a natural-language goal
- Plan includes invoked agents, skipped agents with reasons, task graph, estimates
- Single-pipeline-ingest goals produce a one-task plan whose execution delegates to the existing `m1_plan_agent` (no design-schema duplication)
- Pre-scoped context is correctly assembled for each specialist
- Investigation goals are handled distinctly
- Manual agent override works
- Existing M1.1 `carve plan` / `carve plan --refine` / `carve plan --pipeline` flows continue to work, now mediated by the orchestrator

## Files this spec produces

New:

- `src/carve/core/agents/orchestration/__init__.py`
- `src/carve/core/agents/orchestration/agent.py`
- `src/carve/core/agents/orchestration/shortcuts.py`
- `src/carve/core/agents/orchestration/classifier.py`
- `src/carve/core/agents/orchestration/selector.py`
- `src/carve/core/agents/orchestration/scoping.py`
- `src/carve/core/agents/orchestration/estimator.py`
- `src/carve/core/agents/prompts/orchestration.md`
- `tests/core/agents/orchestration/test_agent.py`
- `tests/core/agents/orchestration/test_shortcuts.py`

Modified (light touches; do not rewrite):

- `src/carve/cli/orchestrator/planner.py` — call the orchestration agent first; for single-pipeline-ingest plans, continue to invoke `m1_plan_agent` via the existing terminator-tool flow.
- `src/carve/core/agents/prompts/m1_plan_agent.md` — unchanged contract; may receive an extra preamble line noting it was invoked via the orchestrator. Optional.
- `src/carve/core/agents/m1_tools.py` — unchanged.
- `src/carve/core/agents/loop.py` — unchanged.

## What this enables

- Specialist agents (dbt, snowflake, quality) only get invoked when needed
- The M1.1 single-pipeline path keeps its cheap, fast shape — no extra round-trip through a multi-agent planner when the goal is "ingest X"
- Token usage stays bounded by pre-scoping
- Users see transparent reasoning about what was and wasn't included
- Future agents slot into the orchestrator's selection layer without changing the M1.1 plan/build/run pipeline
