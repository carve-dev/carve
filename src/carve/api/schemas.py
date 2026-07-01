"""Shared Pydantic response models for the REST API.

Per-router request/response bodies live in their router modules; this holds the
cross-cutting envelopes: the paginated :class:`Page` and the :class:`Problem`
error shape (documented in the OpenAPI schema so error responses are typed).
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from carve.api.pagination import PageResult

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """A page of a collection endpoint's results."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
    #: Omitted (``None``) by default — a full count is expensive on some tables.
    total_count: int | None = None

    @classmethod
    def build(cls, result: PageResult, items: list[T]) -> Page[T]:
        """Assemble a :class:`Page` from a :class:`PageResult` + serialized items."""
        return cls(items=items, next_cursor=result.next_cursor, has_more=result.has_more)


class Problem(BaseModel):
    """RFC 9457 ``application/problem+json`` body (documented error shape)."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None


__all__ = ["Page", "Problem"]
