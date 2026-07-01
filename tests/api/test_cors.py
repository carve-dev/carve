"""CORS: loopback origins (any port) are allowed; non-loopback origins are not."""

from __future__ import annotations

from unittest.mock import MagicMock

from starlette.testclient import TestClient

from carve.api.main import create_app
from carve.core.config.schema import Config


def _client(api_config: Config) -> TestClient:
    return TestClient(create_app(MagicMock(), api_config))


def _preflight(client: TestClient, origin: str):  # type: ignore[no-untyped-def]
    return client.options(
        "/api/v1/runs",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )


def test_loopback_origin_with_port_is_allowed(api_config: Config) -> None:
    client = _client(api_config)
    resp = _preflight(client, "http://127.0.0.1:5173")
    assert resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"


def test_localhost_origin_with_port_is_allowed(api_config: Config) -> None:
    client = _client(api_config)
    resp = _preflight(client, "http://localhost:8080")
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:8080"


def test_non_loopback_origin_is_not_allowed(api_config: Config) -> None:
    client = _client(api_config)
    resp = _preflight(client, "http://evil.example.com")
    assert resp.headers.get("access-control-allow-origin") != "http://evil.example.com"
