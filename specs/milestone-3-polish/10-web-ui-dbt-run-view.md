# M3-10 — Web UI: dbt run view

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 1.5 days
**Dependencies:** M2-12 (run detail screen), M2-05 (manifest reader)

## Purpose

A specialized run detail view for dbt runs that renders the lineage DAG with per-model status, surfaces test results clearly, and provides a "investigate failure" flow that hands the failure context to the dbt agent for diagnosis or repair.

## Why a dedicated view

Generic run detail (M2-12) shows a sequential step list. A dbt run is fundamentally a graph — 50+ models with dependencies, some parallel, some sequential, with tests at various levels. A list view loses the structure. A graph view makes failure investigation immediate ("the upstream of `mart_revenue` failed; here's the chain").

## Layout

```
┌────────────────────────────────────────────────────────────┐
│ Run #xxx · dbt build · Started 2:14 PM · 3m 22s · ✗ failed │
├──────────────────────────────────┬─────────────────────────┤
│                                  │ Right rail              │
│                                  │ ────────────            │
│  [DAG visualization]             │ Selection details       │
│  Models with status colors        │                         │
│  Click → select; hover → preview │ stg_orders               │
│                                  │ ✓ success · 1.2s        │
│                                  │ Tests: 4 passed         │
│                                  │ View SQL · View YAML    │
│                                  │                         │
│                                  │ ─── or for failures ── │
│                                  │ fct_revenue              │
│                                  │ ✗ failed · 18.3s         │
│                                  │ Error: Database error... │
│                                  │ [Investigate failure]    │
│                                  │ View SQL · View YAML    │
│                                  │ View error log           │
└──────────────────────────────────┴─────────────────────────┘
```

The left pane is the DAG. The right rail shows details for the selected node.

## DAG visualization

Use **React Flow** (`@xyflow/react`) for the graph. Reasons:

- Handles complex layouts and zoom/pan natively
- Customizable node rendering (we want status-colored nodes)
- Active maintenance, popular in the data tooling space
- Reasonable bundle size

Layout: use `dagre` or `elk` for automatic layout. The graph flows top-to-bottom (sources → marts) typically.

### Node design

Each model is a small rectangular node:

```
┌────────────────────┐
│ stg_orders         │  ← model name
│ ✓ 1.2s · 4 tests   │  ← status, duration, test count
└────────────────────┘
```

Color encodes status:
- Green border: success
- Red border: failed
- Yellow border: warning (e.g., test warned)
- Blue border + animation: currently running
- Gray: skipped (upstream failed)
- Outlined gray: not yet run

Sources are styled differently (rounded corners, gray fill).

### Edges

Edges show direction (arrow at downstream end). Color matches the upstream node's status to make failure cascades visible.

Critical path through the DAG is highlighted on selection: clicking a failed model highlights its upstream chain back to the root cause.

### Performance

Large dbt projects can have 500+ models. React Flow handles this fine, but rendering all node details is expensive:

- Nodes outside the viewport are simplified (just name)
- Hover triggers full detail
- "Collapse to mart level" toggle hides intermediate models

## Run summary panel (top)

Compact run-level info above the DAG:

```
Run #xxx · dbt build · Started 2:14 PM · 3m 22s · ✗ failed
Models: 47 selected · 38 success · 1 failed · 8 skipped
Tests:  134 selected · 130 pass · 2 fail · 2 warn
Cost:   ~12 Snowflake credits
```

## Right rail: selection details

When a model is selected, the right rail shows:

### For success

- Status, duration
- Test count (passes / fails / warnings)
- Compiled SQL (collapsible viewer with syntax highlighting)
- Source YAML entry
- Generated documentation (if any)
- Recent run history (last 5 runs of this model with durations and outcomes)

### For failure

Same as success, plus:

- Full error output from dbt
- A prominent **Investigate failure** button
- Link to the file in the editor (or copy path)

### For tests

When clicking a test (instead of a model):

- Test SQL (the actual `select`)
- Failure rows preview (if available — dbt stores them in a temp table)
- Test history

## Investigate failure flow

The "Investigate failure" button is the headline feature. It:

1. Opens a dialog asking the user to confirm the investigation
2. Bundles the failure context: error message, model SQL, immediate upstream models, the test that failed (if a test failure)
3. Sends it to the dbt agent as an investigation goal
4. The agent works through diagnosis (using its skills)
5. The result streams back into the UI

Two outcomes the user sees:

**A. Diagnosis only:** the agent identifies the cause and proposes a fix in the chat-like investigation panel. The user can accept (which generates a refinement plan) or decline.

**B. Auto-fix proposal:** for safer cases, the agent generates a Plan with the proposed fix. The user reviews and applies it like any other plan.

Both go through the standard plan/apply flow — investigation never bypasses review.

## API integration

New endpoints needed:

- `GET /api/v1/runs/{id}/dbt-graph` — returns the lineage subgraph for the run, with per-node status from the dbt run results
- `GET /api/v1/runs/{id}/models/{name}` — model-level detail
- `GET /api/v1/runs/{id}/models/{name}/tests` — tests for a model
- `POST /api/v1/runs/{id}/investigate` — start investigation; returns a job_id

The dbt run graph endpoint reads the run's dbt structured logs (parsed during M2-05's runner) to extract per-node status, durations, and errors.

## Components

- `DbtRunView` (page)
- `DbtLineageGraph` (the React Flow wrapper)
- `ModelNode` (custom node)
- `SourceNode`
- `RunSummaryHeader`
- `ModelDetailPanel`
- `TestDetailPanel`
- `InvestigationDialog`
- `InvestigationStream` (streams the agent's reasoning back)

## Tests

- The DAG renders with correct status colors
- Selecting a model populates the detail panel
- Test failures are visible
- Investigation flow opens a dialog and posts to the API
- Layout handles 100+ models without performance issues

Use a fixture run record with a deliberately-mixed run state.

## Acceptance criteria

- The dbt run view shows the complete DAG with per-node status
- Failures are visually obvious; click-through reveals the error
- Investigate-failure produces actionable diagnosis or a fix plan
- Performance is acceptable on 500-model projects

## Files

- `src/carve/ui/src/pages/DbtRunView.tsx`
- `src/carve/ui/src/components/DbtLineageGraph.tsx`
- `src/carve/ui/src/components/dbt/ModelNode.tsx`
- `src/carve/ui/src/components/dbt/SourceNode.tsx`
- `src/carve/ui/src/components/dbt/RunSummaryHeader.tsx`
- `src/carve/ui/src/components/dbt/ModelDetailPanel.tsx`
- `src/carve/ui/src/components/dbt/InvestigationDialog.tsx`
- New endpoints in `src/carve/server/routers/runs.py`
- `tests/server/test_dbt_graph_endpoint.py`

## What this enables

- The fourth and final UI screen in the M3 design
- Failure investigation has a flow with proper context
- Users feel that Carve "understands" their dbt project at the visual level
