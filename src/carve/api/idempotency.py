"""``Idempotency-Key`` middleware for write endpoints.

Guards duplicate-effect writes (POST/PUT/DELETE). On a keyed request it computes
``request_hash = sha256(method + path + body)``, scoped to the authenticated
``(tenant_id, user_id)``, and:

1. cached within 24h with the **same** hash → replay the stored response;
2. cached with a **different** hash → 409 (same key, different body);
3. otherwise → execute, then cache the response (status/body/headers, 24h TTL).

Runs *inside* :class:`carve.api.auth.AuthMiddleware` (added first ⇒ outermost ⇒
runs first), so ``request.state.identity`` is already set. Reading
``await request.body()`` is safe under ``BaseHTTPMiddleware``: Starlette's cached
request replays the buffered body to the downstream endpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from carve.api.errors import problem

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp

    from carve.core.state.store import StateStore

logger = logging.getLogger(__name__)

_WRITE_METHODS = frozenset({"POST", "PUT", "DELETE"})
_RAW_TEXT_KEY = "__raw_text__"

#: Write routes whose response body carries a plaintext secret (bearer token /
#: webhook ``hmac_secret``). These MUST never be cached or replayed: caching would
#: persist a live secret in ``idempotency_keys`` for 24h, and replay would serve a
#: stale one. The middleware short-circuits them before any lookup/store.
_SECRET_RETURNING_POSTS = frozenset({"/api/v1/tokens", "/api/v1/webhooks"})


def _is_secret_returning_write(method: str, path: str) -> bool:
    """Whether ``method``+``path`` returns a plaintext secret (never cacheable)."""
    if method != "POST":
        return False
    if path in _SECRET_RETURNING_POSTS:
        return True
    # POST /api/v1/webhooks/{id}/rotate-secret — match the exact pattern, not a
    # substring (a single non-empty ``{id}`` segment, no over-matching).
    prefix, suffix = "/api/v1/webhooks/", "/rotate-secret"
    if path.startswith(prefix) and path.endswith(suffix):
        middle = path[len(prefix) : -len(suffix)]
        return bool(middle) and "/" not in middle
    return False


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Cache + replay write-endpoint responses keyed by ``Idempotency-Key``."""

    def __init__(self, app: ASGIApp, *, state_store: StateStore) -> None:
        super().__init__(app)
        self._state_store = state_store

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        # Secret-returning writes are never cached or replayed (a live bearer
        # token / hmac_secret must not land in the cache at rest).
        if _is_secret_returning_write(request.method, request.url.path):
            return await call_next(request)

        key = request.headers.get("idempotency-key")
        if request.method not in _WRITE_METHODS or not key:
            return await call_next(request)

        identity = getattr(request.state, "identity", None)
        if identity is None:
            # Unauthenticated: let auth's rejection stand (this shouldn't happen —
            # auth runs first — but never cache an anonymous request).
            return await call_next(request)

        body = await request.body()
        request_hash = _hash_request(request.method, request.url.path, body)
        repo = self._state_store.idempotency_keys

        cached = await run_in_threadpool(
            repo.lookup, identity.tenant_id, identity.user_id, key
        )
        if cached is not None:
            if cached.request_hash != request_hash:
                return problem(
                    409,
                    "idempotency-key-reused",
                    "Idempotency-Key reused with a different request",
                    detail="Same Idempotency-Key used with different request body",
                    instance=request.url.path,
                )
            return _response_from_cache(
                cached.response_status, cached.response_body, cached.response_headers
            )

        response = await call_next(request)
        body_bytes = b"".join([chunk async for chunk in response.body_iterator])

        # Only cache client-visible outcomes; a transient 5xx stays retryable.
        if response.status_code < 500:
            stored_body = _decode_body(body_bytes, response.headers.get("content-type", ""))
            stored_headers = _cacheable_headers(response.headers)
            try:
                await run_in_threadpool(
                    repo.store,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    key=key,
                    request_hash=request_hash,
                    response_status=response.status_code,
                    response_body=stored_body,
                    response_headers=stored_headers,
                )
            except Exception:  # pragma: no cover - best-effort cache write
                logger.warning("failed to cache idempotent response", exc_info=True)

        return Response(
            content=body_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )


def _hash_request(method: str, path: str, body: bytes) -> str:
    return hashlib.sha256(method.encode("utf-8") + path.encode("utf-8") + body).hexdigest()


def _decode_body(body: bytes, content_type: str) -> dict:  # type: ignore[type-arg]
    """Decode a response body for JSONB storage (parsed JSON, else raw-text wrap)."""
    if "application/json" in content_type or "problem+json" in content_type:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
            return {_RAW_TEXT_KEY: body.decode("utf-8", "replace"), "__json__": parsed}
        except ValueError:
            pass
    return {_RAW_TEXT_KEY: body.decode("utf-8", "replace")}


def _cacheable_headers(headers) -> dict:  # type: ignore[no-untyped-def, type-arg]
    """Keep only stable headers (content-type); drop length/date/hop-by-hop."""
    keep = {}
    content_type = headers.get("content-type")
    if content_type:
        keep["content-type"] = content_type
    return keep


def _response_from_cache(status: int, body, headers: dict) -> Response:  # type: ignore[no-untyped-def, type-arg]
    """Reconstruct a stored response, tagging it as an idempotent replay."""
    replay_headers = {**headers, "Idempotency-Replayed": "true"}
    if isinstance(body, dict) and set(body.keys()) == {_RAW_TEXT_KEY}:
        return Response(
            content=body[_RAW_TEXT_KEY],
            status_code=status,
            headers=replay_headers,
            media_type=headers.get("content-type"),
        )
    if isinstance(body, dict) and "__json__" in body and _RAW_TEXT_KEY in body:
        body = body["__json__"]
    return JSONResponse(content=body, status_code=status, headers=replay_headers)


__all__ = ["IdempotencyMiddleware"]
