"""``/api/v1/memory`` — project memory files (``carve memory``).

Read-only over :class:`~carve.core.memory.loader.MemoryLoader`
(``conventions`` / ``standards`` / ``decisions``).
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from carve.api.dependencies import get_project_paths
from carve.api.errors import BadRequest, ResourceNotFound

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.memory.loader import MemoryFile, MemoryLoader

router = APIRouter(prefix="/memory", tags=["memory"])

_KINDS = ("conventions", "standards", "decisions")


class MemoryFileOut(BaseModel):
    kind: str
    path: str
    size_bytes: int
    mtime: datetime


class MemoryFileDetail(MemoryFileOut):
    contents: str


class DecisionIn(BaseModel):
    """Body for ``POST /memory/decisions`` — append a dated decision entry."""

    title: str
    body: str
    date: date_cls | None = None
    reviewers: list[str] = Field(default_factory=list)
    force: bool = False


class DecisionCreatedOut(BaseModel):
    path: str
    kind: str = "decisions"


def _loader(paths: ProjectPaths) -> MemoryLoader:
    from carve.core.memory.loader import MemoryLoader

    return MemoryLoader(paths)


def _load(loader: MemoryLoader, kind: str) -> MemoryFile | None:
    return {
        "conventions": loader.load_conventions,
        "standards": loader.load_standards,
        "decisions": loader.load_decisions,
    }[kind]()


@router.get("", response_model=list[MemoryFileOut])
def list_memory(
    paths: ProjectPaths = Depends(get_project_paths),
) -> list[MemoryFileOut]:
    """List the project memory files that exist (metadata only)."""
    loader = _loader(paths)
    out: list[MemoryFileOut] = []
    for kind in _KINDS:
        memory = _load(loader, kind)
        if memory is not None:
            out.append(
                MemoryFileOut(
                    kind=kind,
                    path=str(memory.path),
                    size_bytes=memory.size_bytes,
                    mtime=memory.mtime,
                )
            )
    return out


@router.post("/decisions", response_model=DecisionCreatedOut, status_code=201)
def memory_append_decision(
    body: DecisionIn,
    paths: ProjectPaths = Depends(get_project_paths),
) -> DecisionCreatedOut:
    """Append a dated decision to ``carve/decisions.md`` (``carve memory append-decision``).

    Fast/synchronous (no agent). ``DecisionAlreadyExists`` → 409; a multiline/empty
    title raises ``ValueError`` (an anti-heading-injection guard) → wrapped 400.
    """
    from carve.core.memory.loader import MemoryLoader
    from carve.core.memory.writer import MemoryWriter

    writer = MemoryWriter(paths, MemoryLoader(paths))
    entry_date = body.date if body.date is not None else date_cls.today()
    try:
        written = writer.append_decision(
            date=entry_date,
            title=body.title,
            body=body.body,
            reviewers=body.reviewers,
            force=body.force,
        )
    except ValueError as exc:
        # Empty / multiline title (heading-injection guard) → 400.
        raise BadRequest(str(exc)) from exc
    return DecisionCreatedOut(path=str(written))


@router.get("/{kind}", response_model=MemoryFileDetail)
def get_memory(
    kind: str,
    paths: ProjectPaths = Depends(get_project_paths),
) -> MemoryFileDetail:
    """Fetch one memory file's contents (``conventions``/``standards``/``decisions``)."""
    if kind not in _KINDS:
        raise BadRequest(f"Unknown memory kind {kind!r}; expected one of {_KINDS}.")
    memory = _load(_loader(paths), kind)
    if memory is None:
        raise ResourceNotFound(f"No {kind} memory file.")
    return MemoryFileDetail(
        kind=kind,
        path=str(memory.path),
        size_bytes=memory.size_bytes,
        mtime=memory.mtime,
        contents=memory.contents,
    )


__all__ = ["router"]
