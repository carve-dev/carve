# M2-13 — Web UI: Pipeline monitor

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M2-12 (workbench scaffolding)

## Purpose

The operational view for pipelines that have been built and are running on schedule. Daily check-in, "what failed last night?" workflow, drill into a specific run, retry from failure.

## Layout

```
┌────────────────────────────────────────────────────────────┐
│ Top bar (shared)                                           │
├────────────────────────────────────────────────────────────┤
│ Pipelines                                          [Filter]│
├────────────────────────────────────────────────────────────┤
│ Pipeline name           Last run    Status      Schedule   │
│ ────────────────────────────────────────────────────────── │
│ salesforce_opps         2h ago      ✓ success   0 */4 *    │
│ stripe_revenue          12m ago     ⟳ running   0 * * *    │
│ marketing_attribution   8h ago      ✗ failed    0 6 * *    │
│ slack_user_events       —           ⏸ paused    @daily     │
│                                                            │
│ Summary metrics (last 24h):                                │
│   • 47 runs total                                          │
│   • 89% success rate                                       │
│   • 3 currently running                                    │
└────────────────────────────────────────────────────────────┘
```

Click a pipeline → drill into its run history. Click a specific run → drill into the run detail with logs and step status.

## Components

### `PipelineList`

The main table. Columns:

- **Pipeline name** — link to drill-in
- **Last run** — relative time + click to open run detail
- **Status** — colored pill with status of last run
- **Schedule** — cron expression rendered humanly ("Every 4 hours")
- **Actions** — pause/resume button, run-now button

Sortable by any column. Filter input at the top filters by name substring.

### `PipelineDetail`

When a pipeline is selected, the right pane (or full-screen view) shows:

- Pipeline metadata (name, schedule, owner, source path)
- Recent runs (table, last 50)
- Statistics: success rate over 7d / 30d, average duration, last failure
- Configuration link (opens the TOML file path; M3 adds inline editor)

### `RunDetail`

When a specific run is opened:

- Run metadata: started, duration, status, cost
- Step-by-step status with timing
- Live log stream (subscribes to the run's WebSocket)
- Action buttons: cancel (if running), retry from failure (if failed)
- Token usage breakdown

### `StatusPill`

Reused everywhere. Statuses and colors:

- `success` — green
- `running` — blue, animated
- `failed` — red
- `cancelled` — gray
- `paused` — yellow
- `crashed` — purple, with attention indicator
- `queued` — light gray
- `pending` — gray

## API integration

Endpoints (already from M2-10):

- `GET /api/v1/pipelines` for the list
- `GET /api/v1/pipelines/{name}` for detail
- `GET /api/v1/runs?pipeline=<name>` for a pipeline's run history
- `GET /api/v1/runs/{id}` for a single run
- `POST /api/v1/pipelines/{name}/runs` to trigger now
- `POST /api/v1/pipelines/{name}/pause` / `/resume`
- `POST /api/v1/runs/{id}/cancel` / `/retry`

WebSocket: `WS /api/v1/ws/all` already used by workbench. The pipeline monitor listens for `run.completed`, `run.failed`, `pipeline.schedule_changed` to refresh queries.

## Polling fallback

If WebSocket is not connected (e.g., proxy strips it), poll `/api/v1/runs?status=running` every 5 seconds. Same UX, slightly slower.

## Empty states

The first time someone opens the pipeline monitor in a fresh project:

```
No pipelines yet.

Pipelines are created when you deploy a plan that includes them.
Try: carve plan "ingest a CSV from a public URL"

[Open Workbench →]
```

## Pause/resume semantics

A paused pipeline:
- Stops being triggered by the scheduler
- Can still be triggered manually via "Run now"
- Existing runs continue
- The TOML file is updated to set `paused = true`

This means pause/resume is a config edit, which becomes a git commit. Document this clearly so the user isn't surprised by a commit.

## Run history filtering

Filters along the top of the run table:
- Status (multi-select)
- Date range
- Pipeline (already implicit if drilled in)
- Search (matches log content — fast for recent, slow for historical; M3 adds indexing)

## Tests

- The pipeline list renders the right columns
- Status pill colors match status
- WebSocket events update the list
- Pause/resume calls the right endpoint
- Empty state renders when no pipelines exist
- Run detail shows step status

## Acceptance criteria

- A user can see all pipelines and their statuses at a glance
- Click-through from list → pipeline → run → logs works
- Pause/resume works
- Manual "Run now" triggers a run
- The screen feels responsive (sub-second updates on status change)

## Files

- `src/carve/ui/src/pages/PipelineMonitor.tsx`
- `src/carve/ui/src/pages/PipelineDetail.tsx`
- `src/carve/ui/src/pages/RunDetail.tsx`
- `src/carve/ui/src/components/PipelineList.tsx`
- `src/carve/ui/src/components/RunHistory.tsx`
- `src/carve/ui/src/components/StatusPill.tsx`
- `src/carve/ui/src/components/LogStream.tsx`

## What this enables

- The day-2 user experience (operating Carve, not just building) has a home
- The dbt run view in M3 is a more specialized version of `RunDetail`
- Pipeline pause/resume is a self-service operation
