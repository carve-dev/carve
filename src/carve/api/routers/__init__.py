"""FastAPI routers — one per resource, each wrapping a shipped service/repo.

Assembled under ``/api/v1`` by :func:`carve.api.main.create_app` (health mounts
at the root). Every router is a thin HTTP surface over an already-shipped
repository, service, or CLI-backing function.
"""
