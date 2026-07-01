"""``/api/v1/skills`` — discovered skill packs (``carve skills``).

Read-only over :class:`~carve.core.skills.pack_discovery.SkillPackLibrary`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from carve.api.dependencies import get_config, get_project_paths
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
    from carve.core.config import Config
    from carve.core.config.paths import ProjectPaths
    from carve.core.skills.packs import SkillPack

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillOut(BaseModel):
    name: str
    description: str
    expects_env: list[str]


class SkillDetail(SkillOut):
    instructions: str


def _discover(paths: ProjectPaths, config: Config) -> list[SkillPack]:
    from carve.core.skills.pack_discovery import discover_pack_roots

    skills_dir = paths.root / config.paths.skills_dir
    return discover_pack_roots(skills_dir=skills_dir).discover()


@router.get("", response_model=list[SkillOut])
def list_skills(
    paths: ProjectPaths = Depends(get_project_paths),
    config: Config = Depends(get_config),
) -> list[SkillOut]:
    """List discovered skill packs."""
    return [
        SkillOut(name=p.name, description=p.description, expects_env=list(p.expects_env))
        for p in _discover(paths, config)
    ]


@router.get("/{name}", response_model=SkillDetail)
def get_skill(
    name: str,
    paths: ProjectPaths = Depends(get_project_paths),
    config: Config = Depends(get_config),
) -> SkillDetail:
    """Fetch one skill pack (including its instructions)."""
    for pack in _discover(paths, config):
        if pack.name == name:
            return SkillDetail(
                name=pack.name,
                description=pack.description,
                expects_env=list(pack.expects_env),
                instructions=pack.instructions,
            )
    raise ResourceNotFound(f"Skill pack {name!r} not found.")


__all__ = ["router"]
