"""``/api/v1/memory`` — project memory files (``carve memory``).

Read-only over :class:`~carve.core.memory.loader.MemoryLoader`
(``conventions`` / ``standards`` / ``decisions``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel

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
