# M2-02 — Orchestration agent

**Milestone:** 2 — Real product
**Estimated effort:** 1.5 days
**Dependencies:** M1-04 (agent loop), M2-01 (plan schema), M2-09 (schema retrieval skills)

## Purpose

The orchestration agent is the only agent that knows about other agents. It takes a goal, classifies it, gathers impact context, picks the right specialist agents, and produces a task graph as a Plan.

This is the most important new agent in M2 because it determines how every other agent gets invoked.

## Responsibilities

1. **Goal classification** — is this a new build, modification, refactor, or investigation?
2. **Impact analysis** — what existing artifacts does this goal touch?
3. **Agent selection** — which specialist agents are needed; which are skipped
4. **Task graph generation** — order, dependencies, expected outputs
5. **Cost and duration estimation**
6. **Skipped-agent transparency** — record why each unselected agent was excluded

## Two-layer agent selection

### Layer 1 — deterministic shortcuts

Cheap pattern matching on the goal text. Implemented in `src/carve/core/agents/orchestration/shortcuts.py`:

| Pattern | Inferred routing |
|---|---|
| Mentions only existing dbt model names → | dbt agent only |
| Mentions a new source system not in any pipeline → | pipeline + snowflake |
| Read-only verbs ("explain", "show", "why") → | no write agents; investigation only |
| Mentions schema/role/warehouse → | include snowflake |
| Mentions tests, freshness, anomalies → | include quality |

Shortcuts produce a *suggestion* that goes into the layer-2 prompt; they don't bind the decision.

### Layer 2 — LLM-based selection

A focused LLM call (Sonnet, not Opus — this is routing, not synthesis):

**System prompt:**
```
You are the routing component of a multi-agent system for data engineering.

Given a user goal, decide:
1. Which specialist agents to invoke (and which to skip)
2. The order they should run
3. A brief description of what each agent should do

Available agents:
- pipeline: generate Python ingestion code for source systems
- dbt: generate, modify, or refactor dbt models, tests, and documentation
- snowflake: manage Snowflake DDL, RBAC, warehouses
- quality: generate tests, freshness checks, anomaly detection rules

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

This shapes the prompt sent to layer 2 and determines what impact analysis to run.

## Impact analysis

For modifications and refactors, run these skills before agent selection:

- `lookup_dbt_model(<name>)` for any model mentioned in the goal
- `get_downstream_dependencies(<model>)` for affected models
- `list_tables(database, schema)` for any schema mentioned

The results become structured context in the layer-2 prompt.

For new builds, less analysis is needed — just check what doesn't already exist.

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

- Goal "make stg_orders incremental" routes to dbt + quality only
- Goal "onboard Salesforce" routes to pipeline + snowflake + dbt + quality
- Goal "explain why mart_revenue is high today" routes to investigation
- Manual `--agents dbt` overrides LLM suggestion
- Investigation goals don't generate task graphs

Use mocked LLM responses for deterministic tests.

## Acceptance criteria

- Orchestration agent produces a Plan from a natural-language goal
- Plan includes invoked agents, skipped agents with reasons, task graph, estimates
- Pre-scoped context is correctly assembled for each specialist
- Investigation goals are handled distinctly
- Manual agent override works

## Files

- `src/carve/core/agents/orchestration/__init__.py`
- `src/carve/core/agents/orchestration/agent.py`
- `src/carve/core/agents/orchestration/shortcuts.py`
- `src/carve/core/agents/orchestration/classifier.py`
- `src/carve/core/agents/orchestration/selector.py`
- `src/carve/core/agents/orchestration/scoping.py`
- `src/carve/core/agents/orchestration/estimator.py`
- `src/carve/core/agents/prompts/orchestration.md`
- `tests/core/agents/orchestration/test_agent.py`

## What this enables

- Specialist agents (dbt, snowflake, quality) only get invoked when needed
- Token usage stays bounded by pre-scoping
- Users see transparent reasoning about what was and wasn't included
