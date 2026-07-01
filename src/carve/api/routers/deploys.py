"""``/api/v1/deploys`` — **command-parity only** (``carve deploy`` / ``carve el``).

Deploy *execution* runs through the CLI (``carve deploy`` / ``carve el deploy``)
and the ``core/deploy/`` surface. There is **no** ``deploys`` table yet — the
deploy-record capability (the ``deploys`` table + PR handoff + deploy-record
list/get endpoints) is Increment 6. This router exists so the CLI→REST parity
test is satisfied and gives clients a stable, documented placeholder; it ships
**no** deploy-record collection. The nearest available history today is a build's
``deployed_at`` (see ``/api/v1/builds``) and run history (``/api/v1/runs``).
"""

from __future__ import annotations

from fastapi import APIRouter

# NOTE(rest-api): no POST here by design — the deploy write-surface is deferred to
# Increment 6 (the deploys table + non-interactive PR handoff). The parity test's
# WRITE_PARITY_EXEMPT records `deploy`/`el deploy`/`el verify` as the explicit,
# reviewed exemption so this deferral can't become a silent gap.
router = APIRouter(prefix="/deploys", tags=["deploys"])


@router.get("", summary="Deploy records (deferred to Increment 6)")
def deploys_placeholder() -> dict[str, str]:
    """Deploy records are not yet a first-class resource.

    Trigger deploys via ``carve deploy`` / ``carve el deploy``; inspect deployed
    artifacts via ``/api/v1/builds`` (``deployed_at``) and ``/api/v1/runs``.
    """
    return {
        "status": "deferred",
        "detail": (
            "Deploy-record endpoints are deferred to Increment 6 (the deploys "
            "table + PR handoff). Use `carve deploy` / `carve el deploy` to run a "
            "deploy; see /api/v1/builds and /api/v1/runs for history."
        ),
    }


__all__ = ["router"]
