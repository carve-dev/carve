"""Auth middleware: valid bearer → authenticated; missing/invalid → 401 problem+json."""

from __future__ import annotations

from unittest.mock import MagicMock

from starlette.testclient import TestClient

from carve.api.main import create_app
from carve.core.config.schema import Config
from tests.api.conftest import GOOD_TOKEN


def _client(fake_store: MagicMock, api_config: Config) -> TestClient:
    return TestClient(create_app(fake_store, api_config))


def test_valid_bearer_is_authenticated(fake_store: MagicMock, api_config: Config) -> None:
    client = _client(fake_store, api_config)
    resp = client.get("/api/v1/tokens", headers={"Authorization": f"Bearer {GOOD_TOKEN}"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_missing_bearer_is_401_problem_json(fake_store: MagicMock, api_config: Config) -> None:
    client = _client(fake_store, api_config)
    resp = client.get("/api/v1/tokens")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"] == "https://carve.dev/errors/missing-bearer-token"
    assert body["status"] == 401


def test_invalid_bearer_is_401_problem_json(fake_store: MagicMock, api_config: Config) -> None:
    client = _client(fake_store, api_config)
    resp = client.get("/api/v1/tokens", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["type"] == "https://carve.dev/errors/invalid-token"


def test_malformed_authorization_header_is_401(fake_store: MagicMock, api_config: Config) -> None:
    client = _client(fake_store, api_config)
    resp = client.get("/api/v1/tokens", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401
    assert resp.json()["type"] == "https://carve.dev/errors/missing-bearer-token"


def test_health_endpoints_need_no_auth(fake_store: MagicMock, api_config: Config) -> None:
    client = _client(fake_store, api_config)
    assert client.get("/healthz").status_code == 200


def test_openapi_needs_no_auth(fake_store: MagicMock, api_config: Config) -> None:
    client = _client(fake_store, api_config)
    resp = client.get("/api/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["openapi"].startswith("3.1")
