"""Carve's REST API — a FastAPI app over the shipped services.

``create_app(state_store, config)`` (in :mod:`carve.api.main`) builds the app
`carve serve` runs: bearer auth + idempotency middleware, problem+json errors,
cursor pagination, the ``/api/v1`` router tree, run streaming (WebSocket/SSE),
and the webhook publisher loop. See the rest-api capability spec.
"""

from carve.api.main import create_app

__all__ = ["create_app"]
