"""OpenAPI customization — tags, metadata, and a stable 3.1 schema.

:func:`customize_openapi` installs a cached ``app.openapi`` that adds ordered tag
metadata (one per router), top-level description/contact/license, and the v1
stability note. FastAPI emits OpenAPI 3.1 by default (0.100+); this keeps that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.openapi.utils import get_openapi

if TYPE_CHECKING:
    from fastapi import FastAPI

#: One entry per router, in a stable presentation order.
_TAGS: list[dict[str, str]] = [
    {"name": "health", "description": "Liveness and readiness probes."},
    {"name": "plans", "description": "Plan design artifacts (`carve plan`)."},
    {"name": "builds", "description": "Build artifacts (`carve build`)."},
    {"name": "runs", "description": "Run listing, logs, and live event streams."},
    {"name": "deploys", "description": "Deploy execution (command-parity only)."},
    {"name": "schedules", "description": "Cron schedules (`carve schedule`)."},
    {"name": "pipelines", "description": "Pipeline assets and lineage (`carve pipelines`)."},
    {"name": "components", "description": "Declared components (`carve component(s)`)."},
    {"name": "targets", "description": "Configured targets (`carve target`)."},
    {"name": "agents", "description": "Discovered agent definitions (`carve agents`)."},
    {"name": "skills", "description": "Discovered skill packs (`carve skills`)."},
    {"name": "mcp-servers", "description": "Configured MCP servers (`carve mcp-servers`)."},
    {"name": "memory", "description": "Project memory files (`carve memory`)."},
    {"name": "metrics", "description": "Cost / run / agent rollups (`carve metrics`)."},
    {"name": "jobs", "description": "The durable work queue."},
    {"name": "workers", "description": "The worker pool (`carve worker`)."},
    {"name": "webhooks", "description": "Webhook subscribers and secret rotation."},
    {"name": "tokens", "description": "API bearer tokens (`carve auth rotate`)."},
]

_DESCRIPTION = (
    "Carve's REST API exposes the full control plane over HTTP. Every CLI command "
    "has a REST counterpart on `/api/v1`. Errors are RFC 9457 "
    "`application/problem+json` with stable `type` URLs; collection endpoints use "
    "opaque cursor pagination (default limit 50, max 200); write endpoints honor "
    "the `Idempotency-Key` header. Run event streams are available over WebSocket "
    "and SSE on `/api/v1/runs/{run_id}/stream`.\n\n"
    "**Stability:** `/api/v1/*` signatures do not break within v1; "
    "backward-compatible additions are allowed. Breaking changes wait for `/api/v2`."
)


def customize_openapi(app: FastAPI) -> None:
    """Install a cached, tag-annotated OpenAPI schema generator on ``app``."""

    def _openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary="Carve control-plane REST API",
            description=_DESCRIPTION,
            routes=app.routes,
            tags=_TAGS,
            contact={"name": "Carve", "url": "https://github.com/carve-dev/carve"},
            license_info={"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
        )
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]


__all__ = ["customize_openapi"]
