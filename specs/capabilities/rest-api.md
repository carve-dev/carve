# REST API: FastAPI app, middleware, routers, streaming, webhooks

> Consolidates the REST surface that earlier specs described endpoint-by-endpoint into a single FastAPI app with cross-cutting middleware (auth, errors, pagination, idempotency), streaming (WebSocket/SSE), and webhook delivery. Per [PRD §6.13 interfaces](../PRD.md), [PRD §6 intro on API parity](../PRD.md), [ARCHITECTURE §8.2 REST API](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 9](../PROJECT_PLAN.md).

## Status

- **Status:** Drafting
- **Depends on:** [state-store](./state-store.md), [dlt-engineer](./dlt-engineer.md), [memory](./memory.md), [runtime](./runtime.md), [pipelines](./pipelines.md). Each of those ships the *services* the routers call into; this spec wires the HTTP surface on top.
- **Blocks:** [mcp-server](./mcp-server.md) (auto-generates MCP tools from this REST surface), [ui](./ui.md), [ask](./ask.md) (adds `/api/v1/asks/*` to this app)

## Goal

Ship the FastAPI app that exposes Carve's full functionality over HTTP. Concretely:

1. The FastAPI application skeleton (`src/carve/api/main.py`) wired into `carve serve` from spec 07
2. **Authentication middleware** validating bearer tokens against the `tokens` table
3. **Error handling** that converts exceptions to `application/problem+json` responses
4. **Pagination** helpers for collection endpoints
5. **Idempotency-Key** support on write endpoints
6. **WebSocket and SSE streaming** for live log and event subscriptions
7. **Webhook publisher** that delivers durable events from the `events` table to user-subscribed URLs with HMAC signing
8. **OpenAPI schema generation** auto-served at `/api/openapi.json` + Swagger UI at `/api/docs`
9. **The full set of v0.1 routers** (plans, builds, runs, deploys, schedules, pipelines, agents, skills, mcp-servers, memory, jobs, workers, metrics, asks*, webhooks)

After this spec lands, every CLI command from earlier specs has its REST counterpart on the v1 API. Spec 10 generates MCP tools from this surface mechanically.

\* the `asks` router is added by spec 12 on top of the scaffolding this spec ships.

## Out of scope

- Hosted-only endpoints under `/api/v1/hosted/...` (tenant management, audit log queries, push-button deploy with approval) — those live in the private hosted repo
- The MCP server itself (lives in spec 10; built as an auto-generated adapter over the routes this spec ships)
- The static HTML UI (lives in spec 11; consumes the read endpoints this spec ships but doesn't ship itself)
- Multi-tenant routing (hosted concern)
- Rate limiting (hosted concern; OSS has none)

## Behavior

### Application skeleton

```python
# src/carve/api/main.py
from fastapi import FastAPI
from .auth import AuthMiddleware
from .errors import ProblemJsonExceptionHandler
from .idempotency import IdempotencyMiddleware
from .openapi_meta import customize_openapi
from .routers import (
    plans, builds, runs, deploys, schedules, pipelines,
    agents, skills, mcp_servers, memory, metrics, workers, webhooks,
)
# asks router added by spec 12

def create_app(state_store, config) -> FastAPI:
    app = FastAPI(
        title="Carve",
        version=carve_version(),
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
    )

    app.add_middleware(IdempotencyMiddleware, state_store=state_store)
    app.add_middleware(AuthMiddleware, state_store=state_store)
    app.add_exception_handler(Exception, ProblemJsonExceptionHandler())

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(plans.router)
    api_v1.include_router(builds.router)
    api_v1.include_router(runs.router)
    api_v1.include_router(deploys.router)
    api_v1.include_router(schedules.router)
    api_v1.include_router(pipelines.router)
    api_v1.include_router(agents.router)
    api_v1.include_router(skills.router)
    api_v1.include_router(mcp_servers.router)
    api_v1.include_router(memory.router)
    api_v1.include_router(metrics.router)
    api_v1.include_router(workers.router)
    api_v1.include_router(webhooks.router)
    app.include_router(api_v1)

    app.add_websocket_route("/api/v1/runs/{run_id}/stream", runs.stream_handler)

    # Health checks
    app.include_router(health.router)        # /healthz, /readyz at the root

    customize_openapi(app)
    return app
```

### Authentication

`src/carve/api/auth.py`:

```python
@dataclass(frozen=True)
class Identity:
    user_id: int                # always 1 in OSS single-user mode
    tenant_id: int              # always 1 in OSS
    token_id: UUID
    scopes: list[str]           # ["*"] in OSS; more granular in hosted

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return problem(401, "missing_bearer_token")
        token_plain = auth_header[7:]
        token_hash = argon2_verify_hash(token_plain)
        identity = await self.state_store.tokens.find_by_hash(token_hash)
        if identity is None:
            return problem(401, "invalid_token")
        request.state.identity = identity
        await self.state_store.tokens.touch_last_used(identity.token_id)
        return await call_next(request)
```

Token rotation:
- `POST /api/v1/tokens` — mint a new token; returns plaintext once; user must save
- `DELETE /api/v1/tokens/{id}` — revoke a token; immediate effect
- `carve auth rotate` (CLI) — mints a new token, writes to `.carve/token`, prints the plaintext for the user

OSS default token (bootstrapped at `carve init`) has scope `["*"]`. In hosted, tokens carry tenant/RBAC claims.

### Error handling

`src/carve/api/errors.py` converts exceptions to `application/problem+json`:

```python
# Example: drift detected during build
{
  "type": "https://carve.dev/errors/config-drift",
  "title": "Plan was generated against a different config",
  "status": 409,
  "detail": "The plan's config_hash does not match the current project config.",
  "instance": "/api/v1/builds",
  "plan_id": "plan_a1b2c3d4",
  "expected_config_hash": "sha256:abc123...",
  "actual_config_hash": "sha256:def456...",
  "recovery_hint": "Run `carve plan --refine` to regenerate against current config."
}
```

Carve defines a structured exception hierarchy (`CarveError` → `ConfigDriftError`, `GuardrailViolationError`, `PipelineAlreadyRunningError`, etc.); the exception handler maps each to a `type` URL + recommended HTTP status. Unrecognized exceptions become 500 with `type = "https://carve.dev/errors/internal"` and a logged stack trace (never returned to the client).

### Pagination

`src/carve/api/pagination.py`:

Collection endpoints accept `?cursor=<opaque>&limit=<n>` (default limit 50, max 200). Responses include:

```json
{
  "items": [...],
  "next_cursor": "eyJsYXN0X2lkIjoiYWJjMTIzIiwiY3JlYXRlZF9hdCI6IjIwMjYtMDUtMTkifQ==",
  "has_more": true,
  "total_count": null   // optional; expensive on some endpoints, omitted by default
}
```

Cursors are opaque base64 of `{"last_id": "...", "created_at": "..."}`; servers can change the encoding without breaking clients. Query semantics: `WHERE (created_at, id) < (cursor.created_at, cursor.last_id) ORDER BY created_at DESC, id DESC LIMIT limit + 1` (the `+ 1` detects has_more without a second query).

### Idempotency

`src/carve/api/idempotency.py`:

Write endpoints (POST, PUT, DELETE) accept `Idempotency-Key: <uuid>` header. Behavior:

1. Compute `request_hash = sha256(method + path + body)`
2. Look up `(idempotency_key, request_hash)` in the `idempotency_keys` table
3. If found within 24h: return the cached response
4. If found within 24h with a different `request_hash`: return 409 with `"detail": "Same Idempotency-Key used with different request body"`
5. Otherwise: process the request; cache the response under `(idempotency_key, request_hash)` with TTL 24h

Idempotency keys are scoped to the authenticated `(tenant_id, user_id)` so two users' keys never collide.

The `idempotency_keys` table:

```sql
CREATE TABLE idempotency_keys (
  tenant_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  key UUID NOT NULL,
  request_hash TEXT NOT NULL,
  response_status INTEGER NOT NULL,
  response_body JSONB NOT NULL,
  response_headers JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, user_id, key)
);
CREATE INDEX ix_idempotency_keys_expires_at ON idempotency_keys(expires_at);
```

A background task in `carve serve` deletes expired rows hourly.

### Streaming

`src/carve/api/streams.py`:

Live log + event streams are exposed on `/api/v1/runs/{run_id}/stream`. Clients can connect via:

- **WebSocket** (`wss://...` or `ws://127.0.0.1:.../`): default upgrade if the client supplies the upgrade header
- **SSE** (`text/event-stream`): default if the client sets `Accept: text/event-stream`

Both deliver the same JSON event stream:

```json
{"event": "step.started", "timestamp": "2026-05-19T14:00:00Z", "data": {...}}
{"event": "log.line", "timestamp": "2026-05-19T14:00:01Z", "data": {"step_id": "ingest_stripe", "level": "INFO", "message": "..."}}
{"event": "step.completed", "timestamp": "2026-05-19T14:01:30Z", "data": {...}}
{"event": "run.completed", "timestamp": "2026-05-19T14:05:00Z", "data": {...}}
```

Subscription mechanics:
- Internal event bus (in-process for OSS, Redis pub/sub in hosted) feeds the stream
- Initial replay: when a client connects, the server first sends a backfill of events for the run since `started_at` (so the client doesn't miss anything that fired before the connection)
- Heartbeat: every 30s, the server sends `{"event": "_keepalive"}` to detect dropped connections
- The stream closes when the run reaches a terminal state (`run.completed`, `run.failed`, `run.cancelled`) — clients should handle the close

### Webhooks

`src/carve/api/webhooks.py`:

Webhooks are user-declared subscribers stored in the `webhooks` table (from spec 07's migration; this spec adds the publisher).

```sql
CREATE TABLE webhooks (
  id UUID PRIMARY KEY,
  url TEXT NOT NULL,
  event_filters JSONB NOT NULL,       -- ["run.failed", "step.failed", ...]
  hmac_secret TEXT NOT NULL,          -- per-webhook, base64-url-encoded random bytes
  active BOOLEAN NOT NULL DEFAULT true,
  tenant_id BIGINT NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE webhook_deliveries (
  id UUID PRIMARY KEY,
  webhook_id UUID NOT NULL REFERENCES webhooks(id),
  event_id BIGINT NOT NULL REFERENCES events(id),
  attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  response_status INTEGER,
  response_body TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,               -- pending | delivered | failed | abandoned
  next_retry_at TIMESTAMPTZ
);
```

Publisher loop:

```python
async def webhook_publisher_loop(state_store, *, interval_s=10.0):
    while not shutdown_requested:
        # Pull unprocessed events that match active webhooks
        deliveries_to_attempt = await state_store.webhook_deliveries.pending_or_due_for_retry()
        for delivery in deliveries_to_attempt:
            await attempt_delivery(delivery)
        await sleep(interval_s)
```

Delivery semantics:
- POST to `webhook.url` with JSON body of the event
- Header `X-Carve-Signature: sha256=<hmac>` where `hmac = HMAC-SHA256(webhook.hmac_secret, body)`
- Header `X-Carve-Event: <event_kind>`, `X-Carve-Delivery-Id: <delivery_id>`
- 5-second timeout; non-2xx response → retry
- Retry schedule: 30s, 1m, 5m, 15m, 1h, 3h (six attempts; then `status = "abandoned"`)
- On success: `status = "delivered"`, `response_status` recorded
- Subscribers verify the HMAC; replays detected via `X-Carve-Delivery-Id` uniqueness

CRUD endpoints:
- `POST /api/v1/webhooks` — create; returns plaintext `hmac_secret` once
- `GET /api/v1/webhooks` — list
- `PATCH /api/v1/webhooks/{id}` — update url, filters, active
- `DELETE /api/v1/webhooks/{id}` — delete
- `POST /api/v1/webhooks/{id}/rotate-secret` — generate new `hmac_secret`; old deliveries with the old secret still verify until purged

### OpenAPI

The OpenAPI schema is auto-generated by FastAPI. `src/carve/api/openapi_meta.py` customizes:

- Tags (one per router; ordered consistently)
- Example payloads for each endpoint (so the Swagger UI shows realistic inputs)
- Error response schemas (consistent across endpoints)
- Top-level descriptions, contact info, license info
- Endpoint-level descriptions cross-linking to the relevant PRD section

Stability commitment: `/api/v1/*` endpoint signatures don't break within v1. Backward-compatible additions (new optional fields, new endpoints) are fine. Breaking changes wait for `/api/v2/*`.

### Health checks

- `GET /healthz` — always returns 200 if the process is up; for liveness probes
- `GET /readyz` — returns 200 only if Postgres is reachable and migrations are at head; 503 otherwise. For readiness probes.

No auth required for either — they're plumbing.

### Server lifecycle (integration with spec 07)

`carve serve` (from spec 07) starts the FastAPI app alongside the scheduler/workers/reaper/archiver:

```python
async def serve_main(config):
    # ... (spec 07's startup sequence)
    api_app = create_app(state_store, config)
    api_server = uvicorn.Server(uvicorn.Config(api_app, host=config.host, port=config.port))
    tasks = [
        asyncio.create_task(api_server.serve()),
        asyncio.create_task(scheduler_loop(state_store)),
        asyncio.create_task(reaper_loop(state_store)),
        asyncio.create_task(archiver_loop(state_store, config.archive)),
        asyncio.create_task(webhook_publisher_loop(state_store)),
    ]
    for _ in range(config.workers):
        tasks.append(asyncio.create_task(worker_loop(...)))
    await wait_for_shutdown(tasks)
```

The FastAPI app shares the process's connection pool and event loop with the runtime — no IPC needed; the in-process event bus from ARCHITECTURE §11.3 makes this efficient.

## Tests

- **Unit (auth):** valid bearer token → authenticated; missing/invalid → 401 with problem+json
- **Unit (errors):** representative `CarveError` subclasses serialize to the expected problem+json shape with stable `type` URLs
- **Unit (pagination):** cursor encode/decode is stable; `has_more` detection via LIMIT+1 trick works
- **Unit (idempotency):** same key + same body → cached response; same key + different body → 409; expired key → fresh execution
- **Unit (openapi):** generated schema includes all v0.1 endpoints; the generated schema validates against the OpenAPI 3.1 spec
- **Integration (lifecycle):** `carve serve` starts; `curl http://127.0.0.1:8765/healthz` returns 200; `curl /api/openapi.json` returns valid schema
- **Integration (streams WebSocket):** start a run via REST; open a WebSocket against `/api/v1/runs/{id}/stream`; receive expected event sequence
- **Integration (streams SSE):** same scenario via SSE; verify keepalive frames every 30s
- **Integration (webhooks delivery):** create webhook subscriber pointing at a fixture server; trigger an event; subscriber receives signed payload; HMAC verifies
- **Integration (webhook retry):** subscriber returns 503; retries follow the schedule; eventually marked abandoned
- **Integration (full coverage):** every CLI command from earlier specs has an integration test that exercises its REST equivalent; the test suite fails CI if any CLI command lacks a REST counterpart (this is the parity-test mechanism from PRD §6 intro)

## Acceptance

- `carve serve` brings up the FastAPI app on the configured host/port
- Every CLI command has a working REST equivalent; the parity test passes
- OpenAPI schema at `/api/openapi.json` is complete, accurate, and matches the deployed routes
- Auth middleware rejects missing/invalid tokens with 401 + problem+json
- Errors throughout the surface conform to problem+json with stable `type` URLs
- Pagination works on all collection endpoints with default limit 50, max 200
- Idempotency-Key prevents duplicate-effect writes within 24h
- WebSocket and SSE both deliver run event streams with backfill + keepalive
- Webhooks deliver with HMAC signature; retries follow the documented schedule; abandoned after 6 attempts
- The static-HTML UI (spec 11) and the MCP server (spec 10) can be built on top of this surface without modifications to this spec

## Design notes

- **Why a separate spec for the REST API rather than ship endpoints in each functional spec?** Because the cross-cutting infrastructure (auth, errors, pagination, idempotency, streaming, webhooks, OpenAPI) is substantial enough to warrant a dedicated spec. Functional specs ship services (`MemoryLoader`, `JobQueue`, `StepExecutor`, etc.); this spec wraps them in HTTP endpoints with consistent middleware. The split also keeps functional specs focused on their domain rather than Web-framework concerns.
- **Why FastAPI rather than Starlette directly or another framework?** FastAPI gives auto-generated OpenAPI from Pydantic types for free, which is a huge multiplier for the headless-by-default story (PRD design decision 5.10). Starlette is the underlying ASGI framework FastAPI uses, so we're already on it. Flask/Django would require manual OpenAPI tooling and lose the Pydantic-as-source-of-truth pattern.
- **Why problem+json instead of JSON:API or a custom error shape?** RFC 9457 (the updated problem+json spec) is the modern standard; it has structured types, supports custom fields, and is widely understood by tooling. JSON:API is overkill for our error needs; custom shapes lose interop with generic error-handling libraries.
- **Why opaque cursor pagination instead of offset/limit?** Because offset/limit has well-known O(n) cost on large collections and inconsistent results under concurrent inserts. Cursor pagination is O(log n) and stable. The opaque base64 encoding lets us change the cursor format later without breaking clients.
- **Why a per-webhook HMAC secret rather than a single per-installation secret?** Because rotating one webhook's secret shouldn't invalidate others. Per-webhook secrets are minor extra storage and give users finer control.
- **Why 6 retry attempts on webhooks (not 3, not 10)?** Six attempts spanning ~5 hours (30s, 1m, 5m, 15m, 1h, 3h) covers most transient outages without flooding subscribers with retries during sustained outages. This is the Stripe / GitHub / standard-webhook-platform convention.
- **Why don't streams require authentication on subsequent messages, only at connection time?** Because the WebSocket/SSE protocols don't have a natural mid-stream re-auth pattern, and once a connection is established, the cost of keeping it open without re-validating is low. Tokens are validated at connection upgrade; subsequent messages on the same connection use the same identity. Token revocation closes existing streams via the same mechanism that rejects new connections.

## Open questions

- **CORS configuration defaults.** *Implementation default.* OSS default: allow `http://127.0.0.1:*` (loopback) for the static UI. Production users override via `[api.cors] allowed_origins = [...]` in `runtime.toml`. Hosted has stricter CORS managed by the control plane.
- **OpenAPI 3.0 vs 3.1.** *Implementation default.* Use 3.1 (FastAPI 0.100+ default); broader tooling supports both. Revisit if a key downstream tool only supports 3.0.
- **API versioning policy: how do we handle v2?** *Implementation default.* When v2 lands, both `/api/v1/*` and `/api/v2/*` run side-by-side for a deprecation window (12 months minimum). v1 endpoints get a `Deprecation` header with a sunset date. Documented in `docs/api-reference.md` even though we don't have a v2 in v0.1.
- **Whether to ship a Python SDK for the REST API.** *Strategy-required.* Probably yes eventually; not in v0.1 (the CLI itself acts as a reference client). A separate `carve-client` Python package could ship post-v0.1 if there's demand. Defer.
- **Telemetry headers.** *Strategy-required.* Should the server return any anonymous telemetry headers (`X-Carve-Version`, response timing) that opt-in clients can use? Coordinate with the broader telemetry question from spec 05.
