"""POST /builds — invokes build_plan; drift → 409; force never bypasses drift."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from carve.api.main import create_app
from carve.core.config.schema import Config
from tests.api.conftest import GOOD_TOKEN

_AUTH = {"Authorization": f"Bearer {GOOD_TOKEN}"}


def _artifact(**kw: object) -> SimpleNamespace:
    base = {
        "build_id": "build_1",
        "run_id": "run_1",
        "pipeline_name": "p",
        "target": "dev",
        "files_written": ["el/p/main.py"],
        "cost_usd": 0.5,
        "success": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def client(fake_store: MagicMock, api_config: Config) -> TestClient:
    fake_store.repository.get_plan.return_value = SimpleNamespace(id="plan_1")  # exists
    return TestClient(create_app(fake_store, api_config), raise_server_exceptions=False)


def test_post_builds_success_returns_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "carve.cli.orchestrator.builder.build_plan", lambda *a, **k: _artifact()
    )
    resp = client.post("/api/v1/builds", json={"plan_id": "plan_1"}, headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["build_id"] == "build_1"
    assert body["run_id"] == "run_1"
    assert body["files_written"] == ["el/p/main.py"]
    assert body["success"] is True


def test_post_builds_plan_not_found_is_404(
    client: TestClient, fake_store: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "carve.cli.orchestrator.builder.build_plan", lambda *a, **k: _artifact()
    )
    fake_store.repository.get_plan.return_value = None
    resp = client.post("/api/v1/builds", json={"plan_id": "ghost"}, headers=_AUTH)
    assert resp.status_code == 404


def test_post_builds_config_drift_is_409_problem_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*a: object, **k: object) -> object:
        from carve.cli.orchestrator.builder import ConfigDriftError

        raise ConfigDriftError("plan_1", plan_hash="sha256:abc", current_hash="sha256:def")

    monkeypatch.setattr("carve.cli.orchestrator.builder.build_plan", _raise)
    resp = client.post("/api/v1/builds", json={"plan_id": "plan_1"}, headers=_AUTH)
    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"] == "https://carve.dev/errors/config-drift"
    assert body["expected_config_hash"] == "sha256:abc"
    assert body["actual_config_hash"] == "sha256:def"


def test_force_does_not_bypass_drift(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # force is phase-only: the drift gate still fires (build_plan checks drift first).
    captured: dict[str, object] = {}

    def _raise(*a: object, **k: object) -> object:
        from carve.cli.orchestrator.builder import ConfigDriftError

        captured.update(k)
        raise ConfigDriftError("plan_1", plan_hash="a", current_hash="b")

    monkeypatch.setattr("carve.cli.orchestrator.builder.build_plan", _raise)
    resp = client.post(
        "/api/v1/builds", json={"plan_id": "plan_1", "force": True}, headers=_AUTH
    )
    assert resp.status_code == 409  # drift wins even with force
    assert captured["force"] is True  # force was forwarded (phase-only, not a drift bypass)
