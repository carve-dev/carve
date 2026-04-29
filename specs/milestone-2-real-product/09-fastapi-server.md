# M2-09 — FastAPI server

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M1-03 (state store), M2-01 (plan store)

## Purpose

Stand up the API server that the web UI will consume and the CLI will (gradually) start talking to. REST endpoints for the resources we have so far (runs, plans, pipelines), single-API-key auth, and static asset serving for the built UI.

## Tech choice

**FastAPI** for these reasons:

- Async out of the box (matters for the WebSocket layer in M2-10)
- Pydantic integration native — schemas are shared with the rest of the codebase
- Automatic OpenAPI generation
- Mature, popular, well-maintained
- Used by `dbt`, `prefect`, and the data ecosystem broadly

## URL surface

All routes prefixed with `/api/v1`.

### Status / health

- `GET /api/v1/status` — server health, version, single-user identity
- `GET /api/v1/version` — Carve version

### Pipelines

- `GET /api/v1/pipelines` — list pipelines
- `GET /api/v1/pipelines/{name}` — pipeline detail
- `POST /api/v1/pipelines/{name}/runs` — trigger a run
- `POST /api/v1/pipelines/{name}/pause` — pause schedule
- `POST /api/v1/pipelines/{name}/resume` — resume schedule

### Runs

- `GET /api/v1/runs` — list, filterable by status, pipeline, date range
- `GET /api/v1/runs/{run_id}` — run detail
- `GET /api/v1/runs/{run_id}/logs` — paginated logs (newline-delimited or JSON)
- `POST /api/v1/runs/{run_id}/cancel` — cancel a running run
- `POST /api/v1/runs/{run_id}/retry` — retry from failure

### Plans

- `GET /api/v1/plans` — list plans
- `GET /api/v1/plans/{plan_id}` — plan detail
- `POST /api/v1/plans` — create new plan from goal (body: `{ "goal": "..." }`)
- `POST /api/v1/plans/{plan_id}/refine` — refine a plan
- `POST /api/v1/plans/{plan_id}/apply` — apply a plan
- `DELETE /api/v1/plans/{plan_id}` — discard a plan

### Agents (read-only in M2; full CRUD in M3 agent studio)

- `GET /api/v1/agents` — list agents
- `GET /api/v1/agents/{name}` — agent definition

### Skills

- `GET /api/v1/skills` — list available skills

### Static UI

- `GET /` — serves `dist/index.html` from the bundled UI

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
    # ... others

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
    limit: int = 50,
    repo: Repository = Depends(get_repo),
    user: str = Depends(require_api_key),
):
    runs = repo.list_runs(status=status, limit=limit)
    return [RunResponse.from_orm(r) for r in runs]
```

## Response schemas

Match the state store models but expose only what's safe:

```python
class RunResponse(BaseModel):
    id: str
    kind: str
    target_id: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error_message: str | None
    cost_usd: float

class PlanResponse(BaseModel):
    id: str
    parent_plan_id: str | None
    goal: str
    created_at: datetime
    expires_at: datetime
    estimates: PlanEstimates
    task_graph: list[Task]
    file_diffs: list[FileDiff]
```

## Plan creation as an async operation

`POST /api/v1/plans` triggers an LLM call, which takes 10-30 seconds. Don't make this a blocking request. Two options:

**Option A (preferred for v0.1):** Return 202 Accepted immediately with a job ID. The client polls `GET /api/v1/jobs/{id}` until ready, then fetches the plan.

**Option B (if simpler):** Make it a long-poll request (60s timeout). Works for the UI; awkward for the CLI.

Go with **Option A**. It generalizes to any long-running operation:

```python
@router.post("/plans", status_code=202)
async def create_plan(body: CreatePlanRequest, repo: Repository = Depends(get_repo)):
    job_id = repo.create_job("plan_generation", body.dict())
    asyncio.create_task(generate_plan_job(job_id, body.goal, repo))
    return {"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}
```

Add a `jobs` table to the state store:

```python
class Job(Base):
    __tablename__ = "jobs"
    id: str
    kind: str  # "plan_generation" | "apply" | etc.
    status: str  # "pending" | "running" | "done" | "failed"
    result_json: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
```

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

- Each endpoint returns the expected schema
- Auth blocks unauthenticated requests
- Auth allows requests with valid key
- Plan creation returns 202 with a job ID
- Job status polling works through the lifecycle

Use `httpx.AsyncClient` against the in-process FastAPI app.

## Acceptance criteria

- `carve serve` starts a server on the default port
- All listed endpoints work and return correct schemas
- Auth works
- The OpenAPI docs page is accessible
- The UI's static assets are served at root

## Files

- `src/carve/server/__init__.py`
- `src/carve/server/app.py`
- `src/carve/server/auth.py`
- `src/carve/server/deps.py`
- `src/carve/server/schemas.py`
- `src/carve/server/static.py`
- `src/carve/server/routers/status.py`
- `src/carve/server/routers/pipelines.py`
- `src/carve/server/routers/runs.py`
- `src/carve/server/routers/plans.py`
- `src/carve/server/routers/agents.py`
- `src/carve/server/routers/skills.py`
- `src/carve/cli/commands/serve.py` (real impl, replaces M1 stub)
- `tests/server/test_app.py`
- `tests/server/test_routers.py`

## What this enables

- The web UI in M2-11/M2-12 has a backend
- The CLI gradually migrates to talking to the server (rather than the DB directly) for parity
- The MCP server in M3 is built as another consumer of these endpoints
