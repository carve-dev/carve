"""POST /plans — invokes generate_plan; 201 + config_hash/cost; PlanGenerationError → 422."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from carve.api.main import create_app
from carve.core.config.schema import Config
from tests.api.conftest import GOOD_TOKEN

_AUTH = {"Authorization": f"Bearer {GOOD_TOKEN}"}


def _plan_row() -> SimpleNamespace:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    return SimpleNamespace(
        id="plan_1",
        parent_plan_id=None,
        goal="ingest stripe",
        config_hash="sha256:abc",
        carve_version="0.0.1",
        task_graph_json={"steps": []},
        file_path="/tmp/plan_1.json",
        phase="drafted",
        pipeline_name=None,
        created_at=now,
        expires_at=now,
    )


@pytest.fixture
def client(fake_store: MagicMock, api_config: Config) -> TestClient:
    fake_store.repository.get_plan.return_value = _plan_row()
    return TestClient(create_app(fake_store, api_config), raise_server_exceptions=False)


def test_post_plans_returns_201_with_config_hash_and_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "carve.cli.orchestrator.planner.generate_plan",
        lambda *a, **k: SimpleNamespace(
            id="plan_1", cost_usd=1.25, tokens_input=1000, tokens_output=400
        ),
    )
    resp = client.post("/api/v1/plans", json={"goal": "ingest stripe"}, headers=_AUTH)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "plan_1"
    assert body["config_hash"] == "sha256:abc"
    assert body["task_graph_json"] == {"steps": []}
    assert body["cost_usd"] == 1.25
    assert body["tokens_input"] == 1000


def test_post_plans_generation_error_is_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*a: object, **k: object) -> object:
        from carve.cli.orchestrator.planner import PlanGenerationError

        raise PlanGenerationError("agent did not submit a plan")

    monkeypatch.setattr("carve.cli.orchestrator.planner.generate_plan", _raise)
    resp = client.post("/api/v1/plans", json={"goal": "nonsense"}, headers=_AUTH)
    assert resp.status_code == 422
    assert resp.json()["type"] == "https://carve.dev/errors/plan-generation"
