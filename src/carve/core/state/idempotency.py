"""The idempotency-keys repository — the ``Idempotency-Key`` replay cache.

Backs :class:`carve.api.idempotency.IdempotencyMiddleware`. A cached row stores
the full response (status/body/headers) under ``(tenant_id, user_id, key)`` with
a 24h ``expires_at``. Mirrors the other state-store repos: shared ``sessionmaker``,
short sync transactions, detached returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from carve.core.state.models import IdempotencyKey

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

#: How long a cached response is replayable.
IDEMPOTENCY_TTL = timedelta(hours=24)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CachedResponse:
    """A stored response for an idempotency key (detached from the ORM)."""

    request_hash: str
    response_status: int
    response_body: dict[str, Any]
    response_headers: dict[str, str]


class IdempotencyKeys:
    """Typed access to the ``idempotency_keys`` table."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def lookup(self, tenant_id: int, user_id: int, key: str) -> CachedResponse | None:
        """Return the cached, non-expired response for a key, or ``None``."""
        with self._session_factory() as session:
            row = session.get(IdempotencyKey, (tenant_id, user_id, key))
            if row is None:
                return None
            if row.expires_at <= _utcnow():
                # Expired: treat as absent so the caller re-executes.
                return None
            return CachedResponse(
                request_hash=row.request_hash,
                response_status=row.response_status,
                response_body=row.response_body,
                response_headers=row.response_headers,
            )

    def store(
        self,
        *,
        tenant_id: int,
        user_id: int,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: dict[str, Any],
        response_headers: dict[str, str],
        now: datetime | None = None,
    ) -> None:
        """Cache a response for ``(tenant_id, user_id, key)`` with a 24h TTL.

        Upserts: an expired row for the same key is overwritten (so a fresh
        execution after expiry replaces the stale cache).
        """
        created = now if now is not None else _utcnow()
        expires = created + IDEMPOTENCY_TTL
        with self._session_factory() as session:
            row = session.get(IdempotencyKey, (tenant_id, user_id, key))
            if row is None:
                session.add(
                    IdempotencyKey(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        key=key,
                        request_hash=request_hash,
                        response_status=response_status,
                        response_body=response_body,
                        response_headers=response_headers,
                        created_at=created,
                        expires_at=expires,
                    )
                )
            else:
                row.request_hash = request_hash
                row.response_status = response_status
                row.response_body = response_body
                row.response_headers = response_headers
                row.created_at = created
                row.expires_at = expires
            session.commit()

    def delete_expired(self, now: datetime | None = None) -> int:
        """Delete rows whose ``expires_at`` has passed. Returns the row count."""
        cutoff = now if now is not None else _utcnow()
        stmt = sa.delete(IdempotencyKey).where(IdempotencyKey.expires_at <= cutoff)
        with self._session_factory() as session:
            result = session.execute(stmt)
            session.commit()
            return int(result.rowcount or 0)  # type: ignore[attr-defined]


__all__ = ["IDEMPOTENCY_TTL", "CachedResponse", "IdempotencyKeys"]
