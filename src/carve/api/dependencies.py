"""Shared FastAPI dependencies for the routers.

The app stashes the :class:`StateStore` and :class:`Config` on ``app.state`` at
:func:`carve.api.main.create_app` time; these accessors read them back.
:func:`get_identity` returns the authenticated principal the
:class:`carve.api.auth.AuthMiddleware` attached to ``request.state``.
:func:`pagination_params` parses + clamps the ``?cursor=&limit=`` query pair.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Query, Request

from carve.api.errors import Unauthorized
from carve.api.pagination import PageParams, make_page_params

if TYPE_CHECKING:
    from carve.core.config import Config
    from carve.core.config.paths import ProjectPaths
    from carve.core.state.store import StateStore
    from carve.core.state.tokens import Identity


def get_state_store(request: Request) -> StateStore:
    """The process-wide :class:`StateStore` bundle."""
    return request.app.state.state_store  # type: ignore[no-any-return]


def get_config(request: Request) -> Config:
    """The merged :class:`Config`."""
    return request.app.state.config  # type: ignore[no-any-return]


def get_project_paths(request: Request) -> ProjectPaths:
    """The :class:`ProjectPaths` (filesystem roots for agents/skills/memory)."""
    return request.app.state.project_paths  # type: ignore[no-any-return]


def get_identity(request: Request) -> Identity:
    """The authenticated principal (set by ``AuthMiddleware``).

    NOTE(rest-api): see issue — hosted-forward per-tenant authorization / scope
    enforcement (``scopes``) belongs here; OSS is single-user full-scope.
    """
    identity = getattr(request.state, "identity", None)
    if identity is None:  # pragma: no cover - middleware guarantees it under /api/v1
        raise Unauthorized("Authentication required.")
    return identity  # type: ignore[no-any-return]


def pagination_params(
    cursor: str | None = Query(default=None, description="Opaque pagination cursor."),
    limit: int | None = Query(
        default=None,
        description="Max items per page (default 50, max 200).",
    ),
) -> PageParams:
    """Parse + clamp the pagination query pair."""
    return make_page_params(cursor, limit)


__all__ = [
    "get_config",
    "get_identity",
    "get_project_paths",
    "get_state_store",
    "pagination_params",
]
