# M2-10 — FastAPI server

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M1-03 (state store), M1.1-06 (pipeline-centric lifecycle: plan / build / run), M2-01 (plan store)

## Update notes (proposal)

This spec was authored before M1.1-06 landed. M1.1-06 reshaped the lifecycle:

- `carve plan` is now design-only (no files written) and a Plan has `phase ∈ {drafted, built}`, `pipeline_name`, and `parent_plan_id`.
- `carve build <plan_id>` is the new code-gen step that materializes `pipelines/<name>/` and creates/updates a `Pipeline` row.
- `carve run <pipeline_name>` is dev execution; re-runnable, no replay guard.
- `carve deploy <pipeline_name>` is reserved for the prod-deploy-via-PR verb (filled in by M2-14).
- Pipelines are first-class with their own SQLite table.

Net REST changes vs. the original M2-10:

- **Removed** `POST /api/v1/plans/{plan_id}/apply` — deploy no longer operates on a plan.
- **Added** `POST /api/v1/plans/{plan_id}/build` — invokes `carve build`.
- **Added** `POST /api/v1/pipelines/{name}/deploy` — invokes `carve deploy` (prod-deploy PR; M2-14 implements the PR mechanics).
- **Removed** `POST /api/v1/pipelines/{name}/pause` and `/resume` — scheduling is M3 territory.
- `POST /api/v1/plans` accepts `{ "goal": "...", "pipeline": "...?" }` matching the `--pipeline` modify flow.
- `POST /api/v1/runs/{run_id}/retry` is dropped in favor of just calling `POST /api/v1/pipelines/{name}/runs` again — `carve run` is re-runnable by design.
- Plan and Pipeline response schemas are reshaped to match the M1.1-06 models.

## Purpose

Stand up the API server that the web UI will consume and the CLI will (gradually) start talking to. REST endpoints for the resources we have so far (pipelines, plans, runs), single-API-key auth, and static asset serving for the built UI.

## Tech choice

**FastAPI** for these reasons:

- Async out of the box (matters for the WebSocket layer in M2-11)
- Pydantic integration native — schemas are shared with the rest of the codebase
- Automatic OpenAPI generation
- Mature, popular, well-maintained
- Used by `prefect` and the data ecosystem broadly

## URL surface

All routes prefixed with `/api/v1`.

### Status / health

- `GET /api/v1/status` — server health, version, single-user identity
- `GET /api/v1/version` — Carve version

### Pipelines

- `GET /api/v1/pipelines` — list pipelines
- `GET /api/v1/pipelines/{name}` — pipeline detail (current build + lineage + recent runs)
- `GET /api/v1/pipelines/{name}/builds` — list this pipeline's build history (most recent first)
- `POST /api/v1/pipelines/{name}/runs` — trigger a dev run (`carve run <name>`)
- `POST /api/v1/pipelines/{name}/deploy` — deploy this pipeline (`carve deploy <name> [--target X]`). M2-14 owns the implementation (5-phase deploy: pre-flight, PR, post-merge DDL/migrations/verify); this endpoint is defined here so the URL surface is stable. Body: `{ "target": "<name>?", "abandon_existing": <bool> }`. Returns 202 with a job id.

### Builds

- `GET /api/v1/builds/{build_id}` — build detail (manifest, target, plan reference, deploy status if shipped)

### Runs

- `GET /api/v1/runs` — list, filterable by status, pipeline, kind, date range
- `GET /api/v1/runs/{run_id}` — run detail
- `GET /api/v1/runs/{run_id}/logs` — paginated logs (uses `since_id` pagination, mirroring the CLI live-tail)
- `POST /api/v1/runs/{run_id}/cancel` — cancel a running run

(Retry is intentionally absent: `POST /api/v1/pipelines/{name}/runs` is re-runnable.)

### Plans

- `GET /api/v1/plans` — list plans (filterable by `phase`, `pipeline_name`, `parent_plan_id`)
- `GET /api/v1/plans/{plan_id}` — plan detail (full task graph, phase, lineage; reachable build via `Build.plan_id` reverse lookup)
- `POST /api/v1/plans` — create new plan from goal. Body: `{ "goal": "...", "pipeline": "<name>?" }`. The optional `pipeline` field maps to `carve plan --pipeline <name>` (modify-existing flow).
- `POST /api/v1/plans/{plan_id}/refine` — refine a draft plan. Body: `{ "feedback": "..." }`. Refuses if the parent plan is already built.
- `POST /api/v1/plans/{plan_id}/build` — invoke `carve build`. Materializes `pipelines/<name>/`, creates a new `Build` row, points `Pipeline.current_build_id` at it, marks the plan `phase="built"`. Returns 202 with a job id (build runs the coordinator + specialists; takes 30–60s).
- `DELETE /api/v1/plans/{plan_id}` — discard a draft plan.

### Agents (read-only in M2; full CRUD in M3 agent studio)

- `GET /api/v1/agents` — list agents
- `GET /api/v1/agents/{name}` — agent definition

### Skills

- `GET /api/v1/skills` — list available skills

### Static UI

- `GET /` — serves `dist/index.html` from the bundled UI

### Superseded / deferred

- ~~`POST /api/v1/plans/{plan_id}/apply`~~ — superseded by `/api/v1/plans/{plan_id}/build` (code-gen) and `/api/v1/pipelines/{name}/deploy` (prod-deploy PR).
- ~~`POST /api/v1/pipelines/{name}/pause`~~, ~~`/resume`~~ — deferred to M3 (scheduling).
- ~~`POST /api/v1/runs/{run_id}/retry`~~ — superseded by re-posting to `/api/v1/pipelines/{name}/runs`.

## Auth

For M2, single-API-key auth:

```python
async def require_api_key(api_key: str = Header(..., alias="X-Carve-API-Key")):
    expected = os.environ.get("CARVE_API_KEY")
    if not expected:
        raise HTTPException(500, "CARVE_API_KEY not set")
    if not hmac.compare_digest(api_key, expected):
        raise HTTPException(401, "Invalid API key")
    return "single-user"
```

The CLI on the same machine reads `CARVE_API_KEY` from env and passes it. The web UI prompts for the key on first load and stores in `localStorage`.

For local development convenience, a `--dev` flag bypasses auth (with a big warning at startup).

## Server structure

`src/carve/server/`:

```
server/
├── __init__.py
├── app.py             # FastAPI app factory
├── auth.py            # API key dependency
├── deps.py            # shared dependencies (config, repo, etc.)
├── routers/
│   ├── __init__.py
│   ├── status.py
│   ├── pipelines.py
│   ├── runs.py
│   ├── plans.py
│   ├── agents.py
│   └── skills.py
├── schemas.py         # Pydantic response models
└── static.py          # static asset serving
```

### App factory

```python
def create_app(config: Config) -> FastAPI:
    app = FastAPI(
        title="Carve API",
        version=carve.__version__,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
    )

    app.include_router(status.router, prefix="/api/v1")
    app.include_router(pipelines.router, prefix="/api/v1")
    app.include_router(plans.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(agents.router, prefix="/api/v1")
    app.include_router(skills.router, prefix="/api/v1")

    # Static UI
    static_dir = Path(__file__).parent.parent / "ui" / "dist"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")

    # CORS for development (UI dev server on different port)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"] if config.server.dev else [],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app
```

### Dependencies

A shared dependency for the repository:

```python
def get_repo() -> Repository:
    config = get_config()  # cached singleton
    engine = create_engine_from_config(config)
    session_factory = create_session_factory(engine)
    return Repository(session_factory)
```

Used in every endpoint:

```python
@router.get("/runs")
async def list_runs(
    status: str | None = None,
    pipeline_name: str | None = None,
    limit: int = 50,
    repo: Repository = Depends(get_repo),
    user: str = Depends(require_api_key),
):
    runs = repo.list_runs(status=status, pipeline_name=pipeline_name, limit=limit)
    return [RunResponse.from_orm(r) for r in runs]
```

## Response schemas

Match the M1.1-06 + M2-01 state store models, exposing only what's safe:

```python
class PipelineResponse(BaseModel):
    name: str
    description: str | None
    pipeline_dir: str
    current_build_id: str | None       # M2-01: replaces current_plan_id
    created_at: datetime
    updated_at: datetime
    last_run_id: str | None
    last_run_status: str | None
    last_run_at: datetime | None

class BuildResponse(BaseModel):
    id: str                            # build_<hex>
    pipeline_name: str
    plan_id: str                       # biographical reference
    target: str                        # the connection target this build was designed against
    created_at: datetime
    manifest: dict                     # { "ddl_files": [...], "migration_files": [...] }
    commit_sha: str | None             # set after deploy ships
    pr_url: str | None                 # set after deploy ships
    deployed_at: datetime | None       # set on successful deploy

class PlanResponse(BaseModel):
    id: str
    parent_plan_id: str | None
    pipeline_name: str | None
    phase: str                         # "drafted" | "built"
    goal: str
    task_graph: dict                   # M2-01 TaskGraph (replaces the legacy `design` blob)
    created_at: datetime

class RunResponse(BaseModel):
    id: str
    kind: str                          # "build" | "run" | "deploy"
    target_id: str
    target: str | None                 # the connection target this run touched (run / deploy only)
    pipeline_name: str | None
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error_message: str | None
    cost_usd: float
```

## Plan creation as an async operation

`POST /api/v1/plans` and `POST /api/v1/plans/{plan_id}/build` both trigger an LLM call and take 10–60 seconds. Don't make these blocking requests. Return 202 Accepted immediately with a job id; the client polls `GET /api/v1/jobs/{id}` until ready, then fetches the resulting plan or pipeline.

```python
@router.post("/plans", status_code=202)
async def create_plan(body: CreatePlanRequest, repo: Repository = Depends(get_repo)):
    job_id = repo.create_job("plan_generation", body.dict())
    asyncio.create_task(generate_plan_job(job_id, body.goal, body.pipeline, repo))
    return {"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}

@router.post("/plans/{plan_id}/build", status_code=202)
async def build_plan(plan_id: str, repo: Repository = Depends(get_repo)):
    job_id = repo.create_job("plan_build", {"plan_id": plan_id})
    asyncio.create_task(build_plan_job(job_id, plan_id, repo))
    return {"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}
```

The `jobs` table on the state store covers any long-running operation:

```python
class Job(Base):
    __tablename__ = "jobs"
    id: str
    kind: str  # "plan_generation" | "plan_build" | "pipeline_deploy" | etc.
    status: str  # "pending" | "running" | "done" | "failed"
    result_json: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
```

`POST /api/v1/pipelines/{name}/runs` does not need to be a job — `LocalVenvRunner` returns a run id immediately and the client tails `/api/v1/runs/{run_id}/logs`. `POST /api/v1/pipelines/{name}/deploy` (M2-14) returns 202 with a job id because PR creation is async.

## carve serve command

`src/carve/cli/commands/serve.py`:

```python
import uvicorn

def serve_command(
    host: str = "127.0.0.1",
    port: int = 8787,
    reload: bool = False,
    dev: bool = False,
):
    config = load_config()
    config.server.host = host
    config.server.port = port
    config.server.dev = dev
    app = create_app(config)
    uvicorn.run(app, host=host, port=port, reload=reload)
```

## Tests

- Each endpoint returns the expected schema (Pipeline, Plan, Run shapes match M1.1-06 models).
- Auth blocks unauthenticated requests; allows requests with a valid key; `--dev` mode bypasses with a logged warning.
- `POST /api/v1/plans` returns 202 with a job id; polling reaches `done` and the resulting plan has `phase="drafted"`.
- `POST /api/v1/plans` with `pipeline` set creates a draft plan that targets the named pipeline (existing files threaded into agent context).
- `POST /api/v1/plans/{plan_id}/refine` rejects refinement of a built plan; on a draft, persists a new plan with `parent_plan_id` set.
- `POST /api/v1/plans/{plan_id}/build` returns 202; on completion, the plan is marked `phase="built"` and a `Pipeline` row exists.
- `POST /api/v1/pipelines/{name}/runs` triggers a real run; the run row appears with `kind="run"` and `pipeline_name=<name>`; re-running succeeds (no replay guard).
- `POST /api/v1/pipelines/{name}/deploy` returns 202 (PR mechanics arrive in M2-14; this test asserts the URL exists and the job is queued).
- `GET /api/v1/runs/{run_id}/logs` paginates with `since_id`.

Use `httpx.AsyncClient` against the in-process FastAPI app.

## Acceptance criteria

- `carve serve` starts a server on the default port (8787).
- All listed endpoints work and return correct schemas.
- Plan/Pipeline/Run response shapes match the M1.1-06 state store models.
- Auth works (header check + `--dev` bypass).
- The OpenAPI docs page is accessible at `/api/v1/docs`.
- The UI's static assets are served at root when present.
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover every endpoint.

## Files this spec produces

New:

- `src/carve/server/__init__.py`
- `src/carve/server/app.py`
- `src/carve/server/auth.py`
- `src/carve/server/deps.py`
- `src/carve/server/schemas.py`
- `src/carve/server/static.py`
- `src/carve/server/routers/__init__.py`
- `src/carve/server/routers/status.py`
- `src/carve/server/routers/pipelines.py`
- `src/carve/server/routers/runs.py`
- `src/carve/server/routers/plans.py`
- `src/carve/server/routers/agents.py`
- `src/carve/server/routers/skills.py`
- `tests/server/__init__.py`
- `tests/server/test_app.py`
- `tests/server/test_auth.py`
- `tests/server/test_routers_pipelines.py`
- `tests/server/test_routers_plans.py`
- `tests/server/test_routers_runs.py`

Modified:

- `src/carve/cli/commands/serve.py` (real impl, replaces M1 stub)
- `src/carve/cli/main.py` (wire serve)
- `pyproject.toml` (add `fastapi`, `uvicorn`, `httpx` runtime/dev deps)
- `src/carve/core/state/models.py` (add `Job` model)
- `src/carve/core/state/repository.py` (job CRUD helpers; reuse pipeline/plan/run helpers from M1.1-06)

## What this enables

- The web UI in M2-12/M2-13 has a backend whose URL surface mirrors the CLI verbs (`plan` / `build` / `run` / `deploy`).
- The CLI gradually migrates to talking to the server (rather than the DB directly) for parity.
- M2-14 (prod-deploy via PR) plugs into a stable `POST /api/v1/pipelines/{name}/deploy` slot.
- The MCP server in M3 is built as another consumer of these endpoints.
- M3 scheduling adds `pause`/`resume` back as `POST /api/v1/pipelines/{name}/schedule/*` endpoints when the time comes.
