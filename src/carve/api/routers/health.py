"""Health checks — ``/healthz`` (liveness) and ``/readyz`` (readiness).

Mounted at the root with **no auth** (they're probes). ``/healthz`` is always
200 if the process is up. ``/readyz`` is 200 only when Postgres is reachable and
the schema is at the latest migration; otherwise 503.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from carve.api.dependencies import get_state_store

if TYPE_CHECKING:
    from carve.core.state.store import StateStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: always 200 while the process is up (no DB touch)."""
    return {"status": "ok"}


def _ready(state_store: StateStore) -> tuple[bool, str]:
    """Return ``(ready, reason)``: Postgres reachable AND migrations at head."""
    engine = state_store.session_factory.kw.get("bind")
    if engine is None:  # pragma: no cover - always bound in practice
        return False, "no database engine bound"
    try:
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception as exc:
        return False, f"database unreachable: {exc.__class__.__name__}"

    try:
        from alembic.script import ScriptDirectory

        from carve.core.state.database import _alembic_config

        head = ScriptDirectory.from_config(_alembic_config(engine)).get_current_head()
    except Exception:  # pragma: no cover - defensive
        logger.warning("could not resolve alembic head", exc_info=True)
        return False, "could not resolve migration head"

    if current != head:
        return False, f"migrations not at head (at {current}, head {head})"
    return True, "ok"


@router.get("/readyz")
def readyz(
    response: Response,
    state_store: StateStore = Depends(get_state_store),
) -> dict[str, str]:
    """Readiness: 200 iff Postgres reachable + schema at head, else 503."""
    ready, reason = _ready(state_store)
    if not ready:
        response.status_code = 503
        return {"status": "not_ready", "reason": reason}
    return {"status": "ok"}


__all__ = ["router"]
