"""OpenAPI: generated schema includes all endpoints + validates against OpenAPI 3.1."""

from __future__ import annotations

from unittest.mock import MagicMock

from openapi_spec_validator import validate

from carve.api.main import create_app
from carve.core.config.schema import Config


def _schema(api_config: Config) -> dict:
    app = create_app(MagicMock(), api_config)
    return app.openapi()


def test_schema_is_openapi_31(api_config: Config) -> None:
    schema = _schema(api_config)
    assert schema["openapi"].startswith("3.1")


def test_schema_validates_against_openapi_spec(api_config: Config) -> None:
    validate(_schema(api_config))  # raises on any spec violation


def test_schema_includes_core_endpoints(api_config: Config) -> None:
    paths = set(_schema(api_config)["paths"])
    for expected in (
        "/healthz",
        "/readyz",
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/logs",
        "/api/v1/runs/{run_id}/stream",
        "/api/v1/plans",
        "/api/v1/builds/{build_id}",
        "/api/v1/schedules",
        "/api/v1/pipelines",
        "/api/v1/metrics/costs",
        "/api/v1/webhooks",
        "/api/v1/tokens",
        "/api/v1/jobs",
        "/api/v1/workers",
    ):
        assert expected in paths, f"missing {expected}"


def test_schema_has_tags_and_metadata(api_config: Config) -> None:
    schema = _schema(api_config)
    tag_names = {t["name"] for t in schema.get("tags", [])}
    assert {"runs", "webhooks", "tokens", "health"}.issubset(tag_names)
    assert schema["info"]["license"]["name"] == "Apache-2.0"
