"""Pagination: cursor encode/decode stability + ``has_more`` via the LIMIT+1 trick."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from carve.api.errors import BadRequest
from carve.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PageParams,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    make_page_params,
    order_candidates,
    paginate,
)


@dataclass
class Row:
    id: str
    created_at: datetime


def _rows(n: int) -> list[Row]:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    # Newest-first, distinct created_at.
    return [Row(id=f"r{i:03d}", created_at=base + timedelta(minutes=n - i)) for i in range(n)]


def test_cursor_round_trips() -> None:
    when = datetime(2026, 5, 19, 14, 0, tzinfo=UTC)
    cursor = decode_cursor(encode_cursor("abc123", when))
    assert cursor.last_id == "abc123"
    assert cursor.created_at == when


def test_cursor_is_opaque_base64() -> None:
    token = encode_cursor("id1", datetime(2026, 1, 1, tzinfo=UTC))
    # Opaque: not human-readable plaintext, but decodes back.
    assert "id1" not in token
    assert decode_cursor(token).last_id == "id1"


def test_malformed_cursor_raises_bad_request() -> None:
    with pytest.raises(BadRequest):
        decode_cursor("not-valid-base64!!!")


def test_clamp_limit_defaults_and_caps() -> None:
    assert clamp_limit(None) == DEFAULT_LIMIT
    assert clamp_limit(5) == 5
    assert clamp_limit(9999) == MAX_LIMIT
    assert clamp_limit(0) == 1


def test_has_more_via_limit_plus_one() -> None:
    rows = order_candidates(_rows(10), id_of=lambda r: r.id, created_of=lambda r: r.created_at)
    params = PageParams(limit=3, cursor=None)
    result = paginate(rows, params, id_of=lambda r: r.id, created_of=lambda r: r.created_at)
    assert len(result.items) == 3
    assert result.has_more is True
    assert result.next_cursor is not None


def test_last_page_has_no_more() -> None:
    rows = order_candidates(_rows(3), id_of=lambda r: r.id, created_of=lambda r: r.created_at)
    params = PageParams(limit=5, cursor=None)
    result = paginate(rows, params, id_of=lambda r: r.id, created_of=lambda r: r.created_at)
    assert len(result.items) == 3
    assert result.has_more is False
    assert result.next_cursor is None


def test_cursor_walks_pages_without_overlap() -> None:
    rows = order_candidates(_rows(10), id_of=lambda r: r.id, created_of=lambda r: r.created_at)
    seen: list[str] = []
    params = make_page_params(cursor=None, limit=4)
    for _ in range(5):  # bounded
        result = paginate(rows, params, id_of=lambda r: r.id, created_of=lambda r: r.created_at)
        seen.extend(r.id for r in result.items)
        if not result.has_more:
            break
        params = make_page_params(cursor=result.next_cursor, limit=4)
    assert len(seen) == 10
    assert len(set(seen)) == 10  # no duplicates across pages
