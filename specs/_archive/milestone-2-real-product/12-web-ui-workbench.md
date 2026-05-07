# M2-12 — Web UI: Workbench

**Milestone:** 2 — Real product
**Estimated effort:** 2 days
**Dependencies:** M2-10 (FastAPI), M2-11 (WebSocket)

## Purpose

Build the workbench — Carve's daily-driver screen. This is where users submit goals, watch active goals progress through their task graphs, and review generated artifacts before deploying. It's the most-used screen in the app.

## Tech stack

- **React 18** with TypeScript
- **Vite** for dev server and build
- **Tailwind CSS** for styling
- **shadcn/ui** for component primitives (Button, Dialog, Tabs, Card, Toast)
- **TanStack Query** for server state management
- **React Router** for screen routing
- **lucide-react** for icons

Why these:
- Modern, well-understood, hireable
- shadcn/ui gives us copy-paste components instead of a black-box library — easy to customize
- TanStack Query handles the polling + WebSocket-update patterns we need

## Project layout

`src/carve/ui/`:

```
ui/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── tailwind.config.js
├── index.html
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── api/
│   │   ├── client.ts        # axios instance with auth header
│   │   ├── runs.ts          # query/mutation hooks
│   │   ├── plans.ts
│   │   └── pipelines.ts
│   ├── ws/
│   │   ├── useRunStream.ts  # subscribe to a run
│   │   └── useGlobalStream.ts
│   ├── components/
│   │   ├── ui/              # shadcn components
│   │   ├── GoalInput.tsx
│   │   ├── ActiveGoals.tsx
│   │   ├── TaskGraph.tsx
│   │   ├── ArtifactPreview.tsx
│   │   ├── PlanSummary.tsx
│   │   └── PlanReviewModal.tsx
│   ├── pages/
│   │   ├── Workbench.tsx
│   │   └── PipelineMonitor.tsx
│   └── lib/
│       ├── format.ts
│       └── status.ts
└── dist/   (build output, served by FastAPI)
```

## The screen layout

Three regions:

```
┌──────────────────────────────────────────────────────────┐
│ Top bar: project name | env switcher | profile menu      │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  [Goal input box  — what do you want to do? ___________] │
│  [active goal cards stack here as user submits goals]    │
│                                                          │
│  Each card:                                              │
│    Goal text + status + duration                         │
│    Task graph (collapsed by default, expandable)         │
│    "Review & deploy" button when plan is ready            │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

Single column. No sidebar in M2 — the workbench is focused.

## Components

### `GoalInput`

A textarea + submit button at the top. On submit:

1. POST `/api/v1/plans` with the goal
2. Get back a job_id
3. Add a new "Active goal" card with status `generating_plan`
4. Subscribe to the global WebSocket
5. When the plan is ready, swap the card to show the plan summary

Keyboard: `Enter` submits; `Shift+Enter` for newline.

### `ActiveGoals`

A list of cards, newest first. Each card has states:

- `generating_plan` — spinner + "Carve is thinking..." (10-30s typical)
- `plan_ready` — plan summary + Review & Deploy button
- `deploying` — task graph with live status per task
- `deployed` — green check + link to PR + total duration
- `failed` — red X + error summary + retry button

### `TaskGraph`

A horizontal task list with status badges. M2 keeps this simple — vertical list of tasks with status pills, not an interactive graph viewer. The actual DAG layout view comes in M3 (dbt run view).

```
[orchestration ✓] → [dbt: modify stg_orders ⟳] → [quality: add tests ⏸] → [PR ⏸]
```

Click a task to expand its log stream below.

### `ArtifactPreview`

When a task generates a file, the preview pane shows the diff (added/modified/deleted with line-level changes). M2 uses a minimal diff renderer:

- Line numbers
- `+` lines green, `-` lines red
- Syntax highlighting for SQL, Python, YAML using `prism` or similar

This is the user's chance to see what the agent generated before it lands in a PR.

### `PlanReviewModal`

When the user clicks Review & Deploy, a modal opens with:

- Goal restated
- Agents involved (and skipped, with reasons)
- Task list
- Estimated cost and duration
- File diffs preview
- "Refine" button (open a refinement input)
- "Deploy" button (commits to running)
- "Discard" button (deletes the plan)

The refinement input is another textarea: "What should we adjust?" → POST `/api/v1/plans/{id}/refine` → swap the modal to a new plan.

## State management

TanStack Query for REST:

```typescript
const { data: runs } = useQuery({
  queryKey: ['runs'],
  queryFn: () => api.runs.list(),
  refetchInterval: 5000,  // also refetch on WS events
})

const submitGoal = useMutation({
  mutationFn: (goal: string) => api.plans.create(goal),
  onSuccess: (data) => {
    // Subscribe to job updates
  }
})
```

For WebSocket events, a custom hook:

```typescript
export function useGlobalStream(onMessage: (msg: WSMessage) => void) {
  useEffect(() => {
    const ws = new WebSocket(`ws://${host}/api/v1/ws/all?api_key=${apiKey}`)
    ws.onmessage = (e) => onMessage(JSON.parse(e.data))
    return () => ws.close()
  }, [])
}
```

When a `run.completed` event fires for a tracked run, invalidate the relevant queries:

```typescript
useGlobalStream((msg) => {
  if (msg.type === 'event' && msg.event_name === 'run.completed') {
    queryClient.invalidateQueries(['runs'])
    queryClient.invalidateQueries(['runs', msg.run_id])
  }
})
```

## Auth

On first load, prompt for the API key (single input dialog), store in `localStorage`. Pass it as a header on all requests and as a query param on WebSocket URLs.

A "Reset key" option in the profile menu clears it.

## Dev server vs production

Dev: Vite serves on port 5173, FastAPI on 8787. CORS is permissive in dev mode.

Production: `npm run build` outputs `dist/`. FastAPI serves it at root. Single port, single origin, no CORS.

Vite dev proxy:

```typescript
// vite.config.ts
server: {
  proxy: {
    '/api': 'http://localhost:8787',
    '/ws': { target: 'ws://localhost:8787', ws: true },
  }
}
```

## Build pipeline

`npm run build` produces `src/carve/ui/dist/`. The Python build copies `dist/` into the wheel so it ships with the package.

A `pre-build` hook in the Python build runs `npm install && npm run build` if `dist/` is missing or stale.

For development, `carve serve --dev` starts FastAPI in dev mode (no static serving), and the developer runs `npm run dev` in `src/carve/ui/` separately.

## Styling and branding

Minimal but distinctive. M2 keeps this very plain:

- Single accent color (teal-600 or similar; finalize in M3)
- Light mode only in M2 (dark mode in M3)
- Inter or system-ui as the font
- Small but consistent spacing scale (Tailwind defaults are fine)

No logo yet — text "Carve" in the top bar.

## Tests

- Snapshot tests for major components (Vitest + React Testing Library)
- A goal submission triggers the expected API call
- Plan review modal renders correctly given a plan object
- WebSocket events update the UI

E2E tests with Playwright wait until M3 (the test setup is sizable).

## Acceptance criteria

- A user can submit a goal, see the plan, review it, and deploy it from the UI
- The active goal card updates live as the run progresses
- File diffs are visible before deploy
- The UI builds and is served by FastAPI in production mode

## Files

- `src/carve/ui/package.json`
- `src/carve/ui/vite.config.ts`
- `src/carve/ui/tsconfig.json`
- `src/carve/ui/tailwind.config.js`
- `src/carve/ui/src/main.tsx`
- `src/carve/ui/src/App.tsx`
- `src/carve/ui/src/api/*.ts`
- `src/carve/ui/src/ws/*.ts`
- `src/carve/ui/src/components/*.tsx`
- `src/carve/ui/src/pages/Workbench.tsx`
- `src/carve/ui/src/pages/PipelineMonitor.tsx` (stub for M2-13)
- `src/carve/ui/src/lib/*.ts`

## What this enables

- The primary workflow (submit goal, review, deploy) has a UI
- The UI is the demoable surface for showing Carve to anyone
- The pipeline monitor (M2-13) reuses these components
