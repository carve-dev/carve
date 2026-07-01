"""Lifecycle: the API comes up; ``/healthz`` → 200; ``/api/openapi.json`` valid.

Runs the real FastAPI app under uvicorn on a free loopback port (the same server
``carve serve`` uses) and curls it with httpx. Skips when Docker is absent (via
``postgres_state_store_url``).
"""

from __future__ import annotations

from pathlib import Path

import httpx
from openapi_spec_validator import validate

from carve.api.main import create_app
from tests.integration._api_support import (
    free_port,
    make_config,
    make_state_store,
    project_paths,
    running_server,
)


def test_api_lifecycle_healthz_and_openapi(
    postgres_state_store_url: str, tmp_path: Path
) -> None:
    port = free_port()
    store = make_state_store(postgres_state_store_url)
    config = make_config(postgres_state_store_url, port=port)
    app = create_app(store, config, project_paths=project_paths(tmp_path))

    with running_server(app, port) as base:
        health = httpx.get(f"{base}/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        # readyz is 200: Postgres reachable + migrations at head.
        ready = httpx.get(f"{base}/readyz")
        assert ready.status_code == 200

        schema = httpx.get(f"{base}/api/openapi.json")
        assert schema.status_code == 200
        body = schema.json()
        assert body["openapi"].startswith("3.1")
        validate(body)


def test_protected_endpoint_requires_token(
    postgres_state_store_url: str, tmp_path: Path
) -> None:
    port = free_port()
    store = make_state_store(postgres_state_store_url)
    config = make_config(postgres_state_store_url, port=port)
    app = create_app(store, config, project_paths=project_paths(tmp_path))
    with running_server(app, port) as base:
        assert httpx.get(f"{base}/api/v1/runs").status_code == 401
