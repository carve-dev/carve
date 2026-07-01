"""``/api/v1/targets`` — configured targets (``carve target``).

Read surface over the resolved ``config.connections`` (the target blocks). Secret
material (``password``/``private_key_path``) is never returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from carve.api.dependencies import get_config
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
    from carve.core.config import Config

router = APIRouter(prefix="/targets", tags=["targets"])


class TargetOut(BaseModel):
    name: str
    dialect: str


class TargetDetail(BaseModel):
    name: str
    dialect: str
    # Non-secret connection attributes (secrets are omitted).
    attributes: dict[str, str | None]


def _targets(config: Config) -> list[TargetOut]:
    out: list[TargetOut] = []
    for name in config.connections.snowflake:
        out.append(TargetOut(name=name, dialect="snowflake"))
    for name in config.connections.duckdb:
        out.append(TargetOut(name=name, dialect="duckdb"))
    return out


@router.get("", response_model=list[TargetOut])
def list_targets(config: Config = Depends(get_config)) -> list[TargetOut]:
    """List configured targets."""
    return _targets(config)


@router.get("/{name}", response_model=TargetDetail)
def get_target(name: str, config: Config = Depends(get_config)) -> TargetDetail:
    """Fetch a target's non-secret connection attributes."""
    snow = config.connections.snowflake.get(name)
    if snow is not None:
        return TargetDetail(
            name=name,
            dialect="snowflake",
            attributes={
                "account": snow.account,
                "user": snow.user,
                "role": snow.role,
                "warehouse": snow.warehouse,
                "database": snow.database,
                "schema": snow.schema_,
                "authenticator": snow.authenticator,
            },
        )
    duck = config.connections.duckdb.get(name)
    if duck is not None:
        return TargetDetail(name=name, dialect="duckdb", attributes={"path": duck.path})
    raise ResourceNotFound(f"Target {name!r} not found.")


__all__ = ["router"]
