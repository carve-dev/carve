"""Bearer-token authentication for the REST API.

:class:`AuthMiddleware` gates every ``/api/v1/*`` request: it extracts the
``Authorization: Bearer <token>`` header, resolves it to an :class:`Identity` via
``state_store.tokens.find_by_token`` (argon2 verification against the active
tokens), stashes the identity on ``request.state``, and stamps ``last_used_at``.
Missing/invalid tokens → 401 problem+json.

``Identity`` and the argon2 hashing / token-generation helpers are defined in the
state layer (:mod:`carve.core.state.tokens`) so ``Tokens.create`` is
self-contained and ``api`` depends on ``state`` (never the reverse); they are
re-exported here as the module's public auth surface. This module also owns the
OSS default-token bootstrap (:func:`ensure_default_token`) and rotation
(:func:`rotate_token`), which write the plaintext to ``.carve/token``.

Health/OpenAPI/docs routes (outside ``/api/v1``) are unauthenticated plumbing.
WebSocket upgrades bypass ``BaseHTTPMiddleware`` entirely (Starlette only runs it
for ``http`` scopes), so the stream handler authenticates at connection time.
"""

from __future__ import annotations

import logging
import os
import stat
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from carve.api.errors import problem

# Re-exported public auth surface (defined in the state layer — see module docs).
from carve.core.state.tokens import (
    Identity,
    generate_token,
    hash_token,
    verify_token,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request
    from starlette.types import ASGIApp

    from carve.core.state.store import StateStore

logger = logging.getLogger(__name__)

#: Everything under this prefix requires a valid bearer token.
_PROTECTED_PREFIX = "/api/v1"


class AuthMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated ``/api/v1/*`` requests; attach the identity otherwise."""

    def __init__(self, app: ASGIApp, *, state_store: StateStore) -> None:
        super().__init__(app)
        self._state_store = state_store

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if not path.startswith(_PROTECTED_PREFIX):
            # Health / OpenAPI / docs are unauthenticated plumbing.
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return problem(
                401,
                "missing-bearer-token",
                "Missing bearer token",
                detail="Provide an `Authorization: Bearer <token>` header.",
                instance=path,
            )
        token_plain = auth_header[len("Bearer ") :].strip()
        identity = await run_in_threadpool(self._state_store.tokens.find_by_token, token_plain)
        if identity is None:
            return problem(
                401,
                "invalid-token",
                "Invalid or revoked token",
                detail="The bearer token was not recognized.",
                instance=path,
            )
        request.state.identity = identity
        # Best-effort last-used stamp; a write hiccup must not fail the request.
        try:
            await run_in_threadpool(self._state_store.tokens.touch_last_used, identity.token_id)
        except Exception:  # pragma: no cover - defensive
            logger.warning("failed to stamp token last_used_at", exc_info=True)
        return await call_next(request)


def _write_token_file(token_path: Path, plaintext: str) -> None:
    """Write the plaintext token to ``token_path`` with 0600 perms (owner-only)."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(plaintext + "\n")
    try:
        os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - platform-dependent (e.g. Windows)
        pass


def ensure_default_token(state_store: StateStore, token_path: Path) -> str | None:
    """Mint the OSS default token (scope ``["*"]``) iff none exists yet.

    Idempotent: with an active default token already present it returns ``None``
    and leaves ``.carve/token`` untouched. Otherwise it mints one, writes the
    plaintext to ``token_path`` (0600), and returns it. Requires the ``tokens``
    table (migrations at head) — call it from ``carve serve`` startup after
    migrations, and best-effort at ``carve init`` when the DB is reachable.
    """
    if state_store.tokens.has_active_default():
        return None
    _token_id, plaintext = state_store.tokens.create(scopes=["*"], is_default=True)
    _write_token_file(token_path, plaintext)
    return plaintext


def rotate_token(state_store: StateStore, token_path: Path) -> str:
    """Mint a fresh full-scope token, rewrite ``.carve/token``, and return it.

    Does not revoke prior tokens (the user may hold others); ``carve auth rotate``
    prints the returned plaintext.
    """
    _token_id, plaintext = state_store.tokens.create(scopes=["*"])
    _write_token_file(token_path, plaintext)
    return plaintext


__all__ = [
    "AuthMiddleware",
    "Identity",
    "ensure_default_token",
    "generate_token",
    "hash_token",
    "rotate_token",
    "verify_token",
]
