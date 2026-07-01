"""POST /runs + POST /runs/{id}/resume — enqueue semantics, label resolution, guards."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from carve.api.main import create_app
from carve.core.config.schema import Config
from tests.api.conftest import GOOD_TOKEN

_AUTH = {"Authorization": f"Bearer {GOOD_TOKEN}"}


def _fake_job(**kw: object) -> SimpleNamespace:
    base = {"id": "job_abc", "pipeline": "p", "target": "dev", "status": "queued"}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def client(fake_store: MagicMock, api_config: Config) -> TestClient:
    fake_store.repository.get_pipeline.return_value = SimpleNamespace(name="p")  # runnable
    fake_store.jobs.enqueue_manual.return_value = _fake_job()
    return TestClient(create_app(fake_store, api_config), raise_server_exceptions=False)


def test_post_runs_resolves_required_label_and_returns_202(
    client: TestClient, fake_store: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The trigger MUST pass a resolved required_label (worker-placement integrity).
    monkeypatch.setattr(
        "carve.cli.commands.serve.resolve_worker_label", lambda *a, **k: "onprem-dbt"
    )
    resp = client.post("/api/v1/runs", json={"pipeline_name": "p"}, headers=_AUTH)
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == "job_abc"
    assert body["status"] == "queued"

    fake_store.jobs.enqueue_manual.assert_called_once()
    _, kwargs = fake_store.jobs.enqueue_manual.call_args
    assert kwargs["required_label"] == "onprem-dbt"
    assert kwargs["trigger"] == "api"


def test_post_runs_defaults_target_from_config(
    client: TestClient, fake_store: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("carve.cli.commands.serve.resolve_worker_label", lambda *a, **k: None)
    client.post("/api/v1/runs", json={"pipeline_name": "p"}, headers=_AUTH)
    args, _ = fake_store.jobs.enqueue_manual.call_args
    assert args[1] == "dev"  # config.project.default_target


def test_post_runs_unknown_pipeline_is_404(
    fake_store: MagicMock, api_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("carve.cli.commands.serve.resolve_worker_label", lambda *a, **k: None)
    fake_store.repository.get_pipeline.return_value = None  # not a pipeline row
    resp = TestClient(create_app(fake_store, api_config), raise_server_exceptions=False).post(
        "/api/v1/runs", json={"pipeline_name": "ghost"}, headers=_AUTH
    )
    assert resp.status_code == 404
    assert resp.json()["type"] == "https://carve.dev/errors/not-found"


def test_post_runs_rejects_path_traversal_pipeline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("carve.cli.commands.serve.resolve_worker_label", lambda *a, **k: None)
    resp = client.post("/api/v1/runs", json={"pipeline_name": "../etc/passwd"}, headers=_AUTH)
    assert resp.status_code == 404


def test_post_runs_nul_byte_pipeline_is_clean_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A NUL/control byte must reject with a clean 404, not a 500 from `.exists()`
    # raising "embedded null byte" at the filesystem touch.
    monkeypatch.setattr("carve.cli.commands.serve.resolve_worker_label", lambda *a, **k: None)
    resp = client.post("/api/v1/runs", json={"pipeline_name": "p\x00.toml"}, headers=_AUTH)
    assert resp.status_code == 404
    assert resp.json()["type"] == "https://carve.dev/errors/not-found"


def test_resume_failed_run_is_202(
    client: TestClient, fake_store: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("carve.cli.commands.serve.resolve_worker_label", lambda *a, **k: None)
    fake_store.repository.get_run.return_value = SimpleNamespace(
        status="failed", pipeline_name="p", target="dev"
    )
    resp = client.post("/api/v1/runs/run_1/resume", headers=_AUTH)
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job_abc"


def test_resume_non_terminal_run_is_409(client: TestClient, fake_store: MagicMock) -> None:
    fake_store.repository.get_run.return_value = SimpleNamespace(
        status="running", pipeline_name="p", target="dev"
    )
    resp = client.post("/api/v1/runs/run_1/resume", headers=_AUTH)
    assert resp.status_code == 409
    assert resp.json()["type"] == "https://carve.dev/errors/conflict"


def test_resume_unknown_run_is_404(client: TestClient, fake_store: MagicMock) -> None:
    fake_store.repository.get_run.return_value = None
    resp = client.post("/api/v1/runs/nope/resume", headers=_AUTH)
    assert resp.status_code == 404
