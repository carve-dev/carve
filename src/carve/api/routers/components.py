"""``/api/v1/components`` — declared components (``carve component`` / ``components``).

Read surface over the resolved ``config.components`` blocks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from carve.api.dependencies import get_config
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
    from carve.core.config import Config
    from carve.core.config.schema import ComponentConfig

router = APIRouter(prefix="/components", tags=["components"])


class ComponentOut(BaseModel):
    name: str
    type: str
    mode: str
    url: str | None
    branch: str | None
    ref: str | None
    path: str | None
    worker_label: str | None


def _to_out(name: str, component: ComponentConfig) -> ComponentOut:
    return ComponentOut(
        name=name,
        type=str(component.type),
        mode=str(component.mode),
        url=component.url,
        branch=component.branch,
        ref=component.ref,
        path=component.path,
        worker_label=component.worker_label,
    )


@router.get("", response_model=list[ComponentOut])
def list_components(config: Config = Depends(get_config)) -> list[ComponentOut]:
    """List declared components (empty in convention-based simple mode)."""
    return [_to_out(name, comp) for name, comp in config.components.items()]


@router.get("/{name}", response_model=ComponentOut)
def get_component(name: str, config: Config = Depends(get_config)) -> ComponentOut:
    """Fetch one declared component by name."""
    component = config.components.get(name)
    if component is None:
        raise ResourceNotFound(f"Component {name!r} not found.")
    return _to_out(name, component)


__all__ = ["router"]
