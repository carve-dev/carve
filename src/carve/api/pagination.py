"""Opaque cursor pagination for collection endpoints.

Cursors are base64-url JSON of ``{"last_id", "created_at"}`` — opaque so the
encoding can change without breaking clients. Query semantics follow the spec:
``WHERE (created_at, id) < (cursor.created_at, cursor.last_id) ORDER BY
created_at DESC, id DESC LIMIT limit + 1`` (the ``+1`` detects ``has_more``
without a second query). ``limit`` defaults to 50, caps at 200.

:func:`paginate` applies that keyset window over a newest-first candidate list.

Implementation note: the candidate list is materialized from the backing repo
(bounded by an internal ceiling) and the keyset filter is applied in-process, so
the *cursor contract* is exact and stable while the SQL stays the repos' existing
``ORDER BY created_at DESC LIMIT n``. Pushing the keyset predicate into each repo
query is a mechanical follow-up when a collection outgrows the ceiling; it does
not change the wire format.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from carve.api.errors import BadRequest

T = TypeVar("T")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
#: Upper bound on rows pulled from a repo before the in-process keyset window.
CANDIDATE_CEILING = 1000


@dataclass(frozen=True)
class Cursor:
    """The decoded keyset position: the last item of the previous page."""

    last_id: str
    created_at: datetime


def encode_cursor(last_id: str, created_at: datetime) -> str:
    """Encode a keyset position to an opaque base64-url string."""
    payload = json.dumps({"last_id": last_id, "created_at": created_at.isoformat()})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(raw: str) -> Cursor:
    """Decode an opaque cursor. Raises :class:`BadRequest` if malformed."""
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
        data = json.loads(decoded)
        return Cursor(
            last_id=str(data["last_id"]),
            created_at=datetime.fromisoformat(data["created_at"]),
        )
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise BadRequest("Malformed pagination cursor.", cursor=raw) from exc


@dataclass(frozen=True)
class PageParams:
    """Validated pagination inputs from the query string."""

    limit: int = DEFAULT_LIMIT
    cursor: Cursor | None = None


def clamp_limit(limit: int | None) -> int:
    """Clamp a requested limit to ``[1, MAX_LIMIT]`` (default when ``None``)."""
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1:
        return 1
    return min(limit, MAX_LIMIT)


def make_page_params(cursor: str | None, limit: int | None) -> PageParams:
    """Build :class:`PageParams` from raw query values (clamp + decode)."""
    return PageParams(
        limit=clamp_limit(limit),
        cursor=decode_cursor(cursor) if cursor else None,
    )


def _is_before(created_at: datetime, item_id: str, cursor: Cursor) -> bool:
    """Whether ``(created_at, id)`` sorts strictly after the cursor in DESC order."""
    if created_at < cursor.created_at:
        return True
    if created_at == cursor.created_at:
        return item_id < cursor.last_id
    return False


@dataclass(frozen=True)
class PageResult:
    """The materialized page: trimmed items + the cursor to the next page."""

    items: list  # type: ignore[type-arg]
    next_cursor: str | None
    has_more: bool


def order_candidates(
    candidates: list[T],
    *,
    id_of: Callable[[T], str],
    created_of: Callable[[T], datetime],
) -> list[T]:
    """Sort ``created_at DESC, id DESC`` — the order :func:`paginate` expects."""
    return sorted(candidates, key=lambda c: (created_of(c), id_of(c)), reverse=True)


def paginate(
    candidates: list[T],
    params: PageParams,
    *,
    id_of: Callable[[T], str],
    created_of: Callable[[T], datetime],
) -> PageResult:
    """Apply the keyset window over a newest-first ``candidates`` list.

    ``candidates`` must already be ordered ``created_at DESC, id DESC``. Returns
    at most ``params.limit`` items plus the ``next_cursor`` (present iff a further
    page exists — the ``LIMIT + 1`` trick).
    """
    filtered = candidates
    if params.cursor is not None:
        cursor = params.cursor
        filtered = [c for c in candidates if _is_before(created_of(c), id_of(c), cursor)]
    window = filtered[: params.limit + 1]
    has_more = len(window) > params.limit
    items = window[: params.limit]
    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(id_of(last), created_of(last))
    return PageResult(items=items, next_cursor=next_cursor, has_more=has_more)


__all__ = [
    "CANDIDATE_CEILING",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "Cursor",
    "PageParams",
    "PageResult",
    "clamp_limit",
    "decode_cursor",
    "encode_cursor",
    "make_page_params",
    "order_candidates",
    "paginate",
]
