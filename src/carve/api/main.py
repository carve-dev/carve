"""``create_app`` — assemble the Carve FastAPI application.

Wires the middleware stack (CORS → auth → idempotency), the problem+json error
handlers, the ``/api/v1`` router tree, the run-stream WebSocket route, the
unauthenticated health routes, and the customized OpenAPI schema. ``carve serve``
constructs the :class:`StateStore` from its existing ``session_factory`` and
hands it here; ``config`` supplies host/port + CORS.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

from carve.api.auth import AuthMiddleware
from carve.api.errors import install_error_handlers
from carve.api.idempotency import IdempotencyMiddleware
from carve.api.openapi_meta import customize_openapi
from carve.api.routers import (
    agents,
    builds,
    components,
    deploys,
    health,
    jobs,
    mcp_servers,
    memory,
    metrics,
    pipelines,
    plans,
    runs,
    schedules,
    skills,
    targets,
    tokens,
    webhooks,
    workers,
)
from carve.core.config.paths import ProjectPaths
from carve.version import __version__

if TYPE_CHECKING:
    from carve.core.config import Config
    from carve.core.state.store import StateStore


def create_app(
    state_store: StateStore,
    config: Config,
    *,
    project_paths: ProjectPaths | None = None,
) -> FastAPI:
    """Build the Carve REST application over ``state_store`` + ``config``."""
    app = FastAPI(
        title="Carve",
        version=__version__,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.state_store = state_store
    app.state.config = config
    app.state.project_paths = project_paths or ProjectPaths.from_root(Path.cwd())

    # Middleware: last-added is outermost. Target request order:
    #   CORS (handles preflight) → Auth (sets identity) → Idempotency (reads it).
    app.add_middleware(IdempotencyMiddleware, state_store=state_store)
    app.add_middleware(AuthMiddleware, state_store=state_store)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.api.cors.allowed_origins,
        allow_origin_regex=config.api.cors.allow_origin_regex,
        allow_credentials=config.api.cors.allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_error_handlers(app)

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(plans.router)
    api_v1.include_router(builds.router)
    api_v1.include_router(runs.router)
    api_v1.include_router(deploys.router)
    api_v1.include_router(schedules.router)
    api_v1.include_router(pipelines.router)
    api_v1.include_router(components.router)
    api_v1.include_router(targets.router)
    api_v1.include_router(agents.router)
    api_v1.include_router(skills.router)
    api_v1.include_router(mcp_servers.router)
    api_v1.include_router(memory.router)
    api_v1.include_router(metrics.router)
    api_v1.include_router(jobs.router)
    api_v1.include_router(workers.router)
    api_v1.include_router(webhooks.router)
    api_v1.include_router(tokens.router)
    # asks router added by spec 12 (scaffolding seam only — no asks router here).
    app.include_router(api_v1)

    # The run event stream shares its path across transports: this WebSocket
    # route plus the SSE ``GET`` registered inside ``runs.router``. Registered on
    # the Starlette router (the handler reads ``path_params`` / ``app.state``
    # directly — no FastAPI dependency injection needed).
    app.router.add_websocket_route("/api/v1/runs/{run_id}/stream", runs.stream_handler)

    # Health probes at the root, unauthenticated (auth only gates ``/api/v1``).
    app.include_router(health.router)

    customize_openapi(app)
    return app


__all__ = ["create_app"]
