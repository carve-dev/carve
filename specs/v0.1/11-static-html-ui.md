# v0.1-11 — Static HTML UI: regenerated-per-run, modeled on `dbt docs`

> Ships the minimal local web UI for OSS self-hosters. Deliberately limited so the upgrade hook to the polished cloud UI stays clear. Per [PRD §6.13 interfaces](../PRD.md), [PRD design decision 5.10 OSS feature-complete; hosted operationally distinct](../PRD.md), [ARCHITECTURE §8.4 local static HTML UI](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 11](../PROJECT_PLAN.md).

## Status

- **Status:** Drafting
- **Depends on:** [v0.1-01 state-store-postgres](./01-state-store-postgres.md), [v0.1-07 runtime](./07-runtime.md), [v0.1-08 multi-step-pipeline](./08-multi-step-pipeline.md), [v0.1-09 rest-api](./09-rest-api.md)
- **Blocks:** nothing structurally; UI is consumer-facing

## Goal

Ship a small, self-contained static HTML UI that lets a self-hoster see what Carve has been doing without learning the REST API. Concretely:

1. **Five page types**: index (recent activity), runs list, single run detail, pipelines list, single pipeline detail
2. **Two additional pages** for diagnostics: agents (registered agent list), skills (registered skill list)
3. **Regeneration triggers** on every state-changing event (run completion, build, deploy, pipeline create/modify, schedule pause/resume)
4. **`carve docs serve`** local-loopback HTTP server that serves the rendered HTML
5. **Self-contained assets** — no CDN dependencies, no analytics, no fonts loaded from the network. Privacy + offline-friendly + reproducible across installs.
6. **Read-only by design** — the rendered HTML never makes API calls. To trigger anything (run a pipeline, refresh memory), users use the CLI / REST / MCP. This is the upgrade hook to the cloud UI.

After this spec lands, a user can `open http://127.0.0.1:8766` (or `carve docs open`) and see what's happening with their pipelines in a friendly format — without needing to know what curl does or what the OpenAPI schema looks like.

## Out of scope

- Live updates (no WebSocket, no SSE in the rendered page; the user reloads to see new state)
- Authentication beyond loopback-only binding (deliberately no login UI; this is local-machine OSS)
- Lineage view rendering (deferred per the resolved audit question — the lineage graph is captured + queryable via [v0.1-19](./19-lineage-graph.md)'s skills/CLI, but not rendered in the static UI until post-v0.1)
- Cost / token usage dashboards (basic metrics surfaced via `/metrics/*` REST endpoints; cloud UI gets the polished version)
- Pipeline / step authoring UI (users author via `carve plan` / `carve build`)
- Anything fancy (the cloud UI in hosted has the polish; this exists to be honestly minimal)

## Files this spec produces

```
src/carve/ui/__init__.py
src/carve/ui/generator.py                               # NEW — orchestrates regeneration: query state store, render templates, write files
src/carve/ui/triggers.py                                # NEW — subscribes to runtime events and re-renders affected pages
src/carve/ui/server.py                                  # NEW — `carve docs serve` static-file server
src/carve/ui/url_helpers.py                             # NEW — link generation (pipelines/<name>.html, run/<id>.html, etc.)

src/carve/ui/templates/                                 # NEW — Jinja2 templates
    base.html                                           # shared layout, nav, footer
    index.html                                          # recent activity dashboard
    runs.html                                           # runs list with filters via query params
    run/detail.html                                     # single run: steps, logs, timings, cost
    pipelines.html                                      # all pipelines + last-run status
    pipeline/detail.html                                # single pipeline: config, schedule, recent runs
    agents.html                                         # built-in + custom agents
    skills.html                                         # built-in + MCP-imported skills
    partials/
        step_status_badge.html
        run_status_badge.html
        cron_friendly.html                              # renders "0 2 * * *" as "daily at 2:00 AM UTC"
        log_block.html

src/carve/ui/static/                                    # NEW — bundled CSS/JS/fonts
    carve.css                                           # ~5-10 KB, hand-written, no framework
    carve.js                                            # tiny, table sort + filter only
    fonts/                                              # system-fallback + one self-hosted font for code blocks
    favicon.svg

src/carve/cli/docs.py                                   # NEW — `carve docs` Typer command group (serve, regen, open)

tests/unit/test_ui_url_helpers.py
tests/unit/test_ui_templates_render.py                  # NEW — fixture state store; render each template; assert HTML is well-formed
tests/unit/test_ui_triggers_subscription.py             # NEW — event emitted → expected pages re-rendered
tests/integration/test_ui_server_serves_files.py        # NEW — start server, GET pages, assert 200 + expected content
tests/integration/test_ui_regeneration_end_to_end.py    # NEW — run a pipeline, verify index/runs/pipeline pages reflect it

docs/ui.md                                              # NEW — what the UI does, where it falls short, upgrade hook to cloud UI
```

## Behavior

### Output directory

Rendered HTML lives at `.carve/ui/` (gitignored). The structure mirrors the URL paths:

```
.carve/ui/
├── index.html
├── runs.html
├── run/
│   ├── 4f6a... .html        # one file per run, keyed by run_id
│   └── ...
├── pipelines.html
├── pipeline/
│   ├── stripe.html
│   └── ...
├── agents.html
├── skills.html
└── static/
    └── (copied from src/carve/ui/static/)
```

`carve docs serve` serves this directory as plain static files. The server has no API calls back; everything is pre-rendered.

### Pages

#### `index.html` — recent activity

A one-page dashboard:

- Top-level metrics: total pipelines, runs in the last 24h (succeeded/failed/partial), cost in the last 7d
- Recent runs: 20 most recent, with status, pipeline, target, duration, link to detail
- Recent decisions: 5 most recent entries from `carve/decisions.md`
- Active alerts: any pipelines with `failed` runs in the last 24h, prominently surfaced

The page is the user's "what just happened" view; the cloud UI eventually replaces it with a live, multi-tenant version.

#### `runs.html` — full run history

Tabular view of all runs in the active `runs` table (not archive). Columns: status, pipeline, target, trigger (scheduled/manual/api/mcp), started_at, duration, cost. Sortable client-side via tiny JS; filterable via URL query params (`?status=failed`, `?pipeline=stripe`).

Renders the first 200 rows; for older runs, the page suggests `carve runs list --since 90d --limit 1000` (the active runs table is bounded by the archive window from spec 07).

#### `run/<id>.html` — single run detail

For one run:

- Summary header: pipeline, target, trigger, status, started/finished, duration, cost (tokens + USD)
- Step DAG: visual representation (just a vertical list with depends_on indicated by indentation; no fancy graph layout)
- For each step: status badge, type, started/finished, duration, error message (if any), expandable log block, named outputs
- Links to: the parent build (`build/<id>.html`?), the parent plan, the pipeline definition

#### `pipelines.html` — all pipelines

Table of every pipeline registered in the project:

- Name, schedule (rendered as "daily at 2 AM"), default target, paused?, last run status, last run started_at, links to detail

#### `pipeline/<name>.html` — single pipeline detail

For one pipeline:

- Current `pipelines/<name>.toml` rendered with syntax highlighting (Python prettyprint via Pygments; no JS-side highlighting)
- Schedule details + next fire time
- Step DAG (same format as run/<id>.html)
- Recent runs table (last 20)
- Per-pipeline memory sidecar (`pipelines/<name>.md`) if it exists, rendered as Markdown

#### `agents.html` — registered agents

List of built-in + custom agents with: name, model, allowed_skills, specialization classifications, max_iterations.

#### `skills.html` — registered skills

List of built-in + MCP-imported skills with: name, source (built-in vs MCP server name), input schema preview, description.

### Regeneration triggers

`src/carve/ui/triggers.py` subscribes to the in-process event bus from ARCHITECTURE §11.3 (spec 07's `events` table is durable; this layer subscribes to the live event stream for prompt regeneration).

| Event                          | Pages regenerated                                          |
|--------------------------------|------------------------------------------------------------|
| `run.queued`                   | `index.html`, `runs.html`, `pipeline/<name>.html`           |
| `run.started`                  | `index.html`, `runs.html`, `run/<id>.html`, `pipeline/<name>.html` |
| `run.completed` / `failed`     | `index.html`, `runs.html`, `run/<id>.html`, `pipeline/<name>.html` |
| `step.completed` / `failed`    | `run/<id>.html` only (avoid re-rendering the world on each step) |
| `plan.created`                 | `index.html`                                               |
| `build.completed`              | `pipeline/<name>.html` (TOML may have changed)              |
| `deploy.opened` / `merged`     | `pipeline/<name>.html`                                     |
| `schedule.paused` / `resumed`  | `pipeline/<name>.html`, `pipelines.html`                   |

Regeneration is debounced (default 500ms): if multiple events for the same page fire in rapid succession, only one regeneration runs. This avoids hammering disk during burst-rate step completions in a fan-out pipeline.

### `carve docs serve` and friends

```
carve docs serve [OPTIONS]
  --host TEXT          Host to bind (default: 127.0.0.1; warns on 0.0.0.0)
  --port INTEGER       Port (default: 8766)
  --no-auto-regen      Don't run a one-shot regen before starting the server
  --watch              Re-render on event-bus events (default: true)

carve docs regen [OPTIONS]
  --page TEXT          Only regenerate the named page (default: all)
                       Useful for "I edited a template, regenerate"

carve docs open                Open the index page in the default browser
```

The server is small (`http.server`-equivalent via `aiohttp` or similar): serves files from `.carve/ui/`, returns 404 for missing files, no auth, no API. Stops on Ctrl-C.

### Self-contained assets

`src/carve/ui/static/`:

- **`carve.css`** — hand-written, 5–10 KB minified. Variables for a sane color palette. Mobile-friendly (responsive grid). Light + dark modes via `prefers-color-scheme` (no toggle to add JS complexity).
- **`carve.js`** — tiny (~1 KB minified). Implements: table sorting on click, filter inputs that hide non-matching rows, expand/collapse of long log blocks. No framework, no module bundler, no build step. Plain ES2020 in a single `<script>`.
- **Fonts** — system font stack for body text + headings (`-apple-system, BlinkMacSystemFont, ...`); one self-hosted monospace font for code/logs (~30 KB).
- **Favicon** — single SVG (Carve logo placeholder).

No CDN deps, no Google Fonts, no analytics, no third-party JS. Honest privacy default + fully offline-capable + reproducible across air-gapped installs.

### Read-only by design

The rendered HTML never makes an API call. Buttons that look like they'd trigger actions (e.g., "rerun this pipeline") instead show the CLI command the user would run:

```html
<div class="cli-hint">
  To rerun this pipeline: <code>carve run stripe --resume {{ run.id }}</code>
</div>
```

This is deliberate — the OSS UI is for observation, not interaction. The cloud UI in hosted does the polished interactive version. Per design decision 5.10, this preserves the upgrade hook honestly.

### Markdown rendering for memory sidecars

The pipeline detail page renders `pipelines/<name>.md` as Markdown if it exists. Same for `el/<name>/NOTES.md` on whatever page surfaces EL artifacts. Markdown rendering via `markdown-it-py` or `mistune` (pure Python; no JS). Sandboxed — no embedded HTML, no `<script>` execution.

### Performance characteristics

- Per-page render: < 100ms for typical state (a few hundred runs, dozens of pipelines)
- Full-site regeneration (all pages): < 5 seconds on a typical state store
- Disk usage: under 10 MB for the rendered tree at a year's worth of OSS-scale state (most pages are HTML, no images)

### Failure modes

- **State store unreachable**: regeneration emits a warning to `carve serve` logs; rendered tree gets a generic "couldn't connect to state store" banner. `carve docs serve` continues to serve whatever's already on disk.
- **Template error**: regeneration of the affected page fails; logs the traceback; the previously-rendered version stays on disk; other pages unaffected.
- **Disk full**: regeneration fails; logs the error; existing rendered tree is unchanged.

## Tests

- **Unit (url_helpers):** generates expected URL paths for runs, pipelines, agents, etc.
- **Unit (templates render):** for each template, given a fixture state, the rendered HTML is well-formed and contains the expected fields (verify via `html5lib` parsing + assertion on text)
- **Unit (trigger subscription):** event emission triggers re-rendering of the right pages and not the wrong ones; debouncing works
- **Integration (server):** `carve docs serve` against a fixture rendered tree; GET each page returns 200 with expected content; missing files return 404 with a sensible message
- **Integration (regeneration end-to-end):** trigger a fixture run via the REST API; verify the index, runs, and pipeline pages are regenerated within the debounce window
- **Integration (markdown):** a `pipelines/<name>.md` with markdown content renders on the pipeline detail page; HTML injection attempts in the markdown are escaped
- **Integration (asset bundling):** all static files referenced by templates exist in `static/`; no broken references; HTML validates against W3C validator (mocked locally via `html5lib`)

## Acceptance

- `carve docs serve` brings up the UI at `http://127.0.0.1:8766`
- All seven pages render correctly against a fresh-installed Carve project with one or two test pipelines
- Run a pipeline; within 500ms of completion, the index + runs + pipeline pages reflect the new state on next page reload
- The UI surface has zero CDN dependencies and zero outbound network calls
- Light and dark modes work via OS-level `prefers-color-scheme`
- The HTML pages pass W3C validation
- Disk usage stays under 10 MB for a year of typical OSS-scale state
- The cloud UI's eventual launch is a clear upgrade — the OSS UI is honestly small enough that "I want live updates and richer interaction" is a natural reason to subscribe

## Design notes

- **Why static HTML regenerated per event rather than a live SPA?** Per design decision 5.10. A live SPA in OSS would be (a) substantial UX work that competes with the hosted product's cloud UI, and (b) require backend code to serve dynamic data, complicating the OSS install. Static regeneration is dramatically simpler to build, maintain, and reason about — and it makes the upgrade hook to the cloud UI honest ("you get a live UI when you upgrade").
- **Why `dbt docs serve` as the reference model?** Because it's the well-understood baseline for "open-source data tool with a minimal local UI." Users coming from dbt will recognize the shape and have appropriate expectations. The cloud UI in hosted is the analog of `dbt Cloud`'s interface.
- **Why no JS framework?** Three reasons. (1) Hand-written CSS + 1 KB of vanilla JS is enough for tables, filters, and expandable logs. (2) Building tooling (webpack, bundlers, etc.) adds significant install complexity for marginal benefit. (3) The polished UI lives in the hosted product where React/Next.js is the right choice — keeping OSS minimal preserves that distinction.
- **Why no auth in OSS?** Because loopback-only binding is a sufficient security boundary for a single-user OSS install. Adding login would imply multi-user, which is a hosted concern.
- **Why pre-render rather than render on request?** Because pre-rendering means GET requests are filesystem-fast and the server has no DB connection to manage. The trade-off is that pages can be stale up to the debounce window after an event — acceptable for this UI's purpose. The cloud UI uses live data because that's its value prop.
- **Why no live updates via SSE/WebSocket even though the REST API supports them?** Same reason. A live UI makes the OSS surface compete with the hosted product on the wrong axis. The honest answer to "I want live updates" is "subscribe to the hosted product or roll your own WebSocket client against the REST stream." Both are perfectly reasonable.
- **Why a Python Markdown renderer rather than a JS-side one?** No JS = no XSS surface from markdown. Pure Python rendering server-side keeps the security model simple.

## Open questions

- **Whether to include a "step graph" visualization beyond the indented-list rendering.** *Implementation default.* No graph rendering library in v0.1; the indented-list works and avoids pulling in d3/cytoscape/vis-network etc. If users find the list confusing, add a simple SVG-based graph later. Cloud UI gets the polished interactive graph.
- **Whether the UI should also surface the OpenAPI schema (e.g., embed Swagger UI).** *Implementation default.* No; `/api/docs` from the REST server already serves Swagger UI. The static UI links to it from the agents/skills/footer pages.
- **Whether to support a custom theme (CSS override).** *Implementation default.* Not in v0.1; theming requests get tracked as a post-v0.1 enhancement. Users who really want different styling can override `.carve/ui/static/carve.css` directly (their override survives regen because the asset copy is idempotent — same content = no write).
- **Whether to render runs in archive tables (older than the active window).** *Implementation default.* No in v0.1; the active runs table is sufficient for the dashboard. Surfacing archived runs requires query-by-pipeline-and-date which is a different UX. Hosted gets it; OSS users with archive needs use the REST `/metrics/runs?since=` endpoint.
