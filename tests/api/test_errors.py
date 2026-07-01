"""Errors: representative real exceptions → problem+json with stable ``type`` URLs."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from carve.api.errors import BadRequest, ResourceNotFound, install_error_handlers


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/drift")
    def _drift() -> None:
        from carve.cli.orchestrator.builder import ConfigDriftError

        raise ConfigDriftError("plan_a1b2", plan_hash="sha256:abc", current_hash="sha256:def")

    @app.get("/config")
    def _config() -> None:
        from carve.core.config.exceptions import ConfigError

        raise ConfigError("bad value", field="connections.snowflake.dev.account", hint="fix it")

    @app.get("/already-running")
    def _running() -> None:
        from carve.core.state.job_queue import PipelineAlreadyRunning

        raise PipelineAlreadyRunning("ingest is already running")

    @app.get("/unsafe-ddl")
    def _ddl() -> None:
        from carve.core.deploy.ddl_applier import UnsafeDdlError

        raise UnsafeDdlError(index=2, label="DROP TABLE")

    @app.get("/not-found")
    def _nf() -> None:
        raise ResourceNotFound("Run 'xyz' not found.")

    @app.get("/bad")
    def _bad() -> None:
        raise BadRequest("nope", cursor="junk")

    @app.get("/boom")
    def _boom() -> None:
        raise ValueError("internal detail that must not leak")

    return TestClient(app, raise_server_exceptions=False)


def test_config_drift_round_trips_to_409(client: TestClient) -> None:
    resp = client.get("/drift")
    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"] == "https://carve.dev/errors/config-drift"
    assert body["status"] == 409
    assert body["plan_id"] == "plan_a1b2"
    assert body["expected_config_hash"] == "sha256:abc"
    assert body["actual_config_hash"] == "sha256:def"
    assert "recovery_hint" in body
    assert body["instance"] == "/drift"


def test_config_error_carries_field_and_hint(client: TestClient) -> None:
    body = client.get("/config").json()
    assert body["type"] == "https://carve.dev/errors/config-invalid"
    assert body["status"] == 400
    assert body["field"] == "connections.snowflake.dev.account"
    assert body["recovery_hint"] == "fix it"


def test_pipeline_already_running_is_409(client: TestClient) -> None:
    resp = client.get("/already-running")
    assert resp.status_code == 409
    assert resp.json()["type"] == "https://carve.dev/errors/pipeline-already-running"


def test_unsafe_ddl_is_400(client: TestClient) -> None:
    resp = client.get("/unsafe-ddl")
    assert resp.status_code == 400
    assert resp.json()["type"] == "https://carve.dev/errors/unsafe-ddl"


def test_resource_not_found_is_404(client: TestClient) -> None:
    resp = client.get("/not-found")
    assert resp.status_code == 404
    assert resp.json()["type"] == "https://carve.dev/errors/not-found"


def test_bad_request_carries_custom_fields(client: TestClient) -> None:
    resp = client.get("/bad")
    assert resp.status_code == 400
    body = resp.json()
    assert body["type"] == "https://carve.dev/errors/bad-request"
    assert body["cursor"] == "junk"


def test_unmapped_exception_is_500_without_leaking(client: TestClient) -> None:
    resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["type"] == "https://carve.dev/errors/internal"
    # The internal detail must never be returned to the client.
    assert "internal detail that must not leak" not in str(body)
