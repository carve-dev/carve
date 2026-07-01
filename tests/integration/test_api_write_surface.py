"""Write-surface end-to-end over the app: plan/build/run/memory triggers + idempotency.

Real :class:`StateStore` (Postgres) + real ``IdempotencyMiddleware``; the
multi-minute agent orchestrators (``generate_plan``/``build_plan``) are
monkeypatched to fast fakes so no live model is needed. Skips cleanly without
Docker (via ``postgres_state_store_url``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from carve.api.main import create_app
from carve.core.config.paths import ProjectPaths
from carve.core.state.models import Plan
from carve.core.state.store import StateStore
from tests.integration._api_support import make_config, make_state_store


def _client(store: StateStore, tmp: Path) -> tuple[TestClient, dict[str, str]]:
    _tid, token = store.tokens.create(scopes=["*"])
    app = create_app(store, make_config("x"), project_paths=ProjectPaths.from_root(tmp))
    client = TestClient(app, raise_server_exceptions=False)
    return client, {"Authorization": f"Bearer {token}"}


def _save_plan(store: StateStore, plan_id: str, config_hash: str = "") -> None:
    now = datetime.now(UTC)
    store.repository.save_plan(
        Plan(
            id=plan_id,
            goal="g",
            config_hash=config_hash,
            carve_version="0.0.1",
            task_graph_json={"steps": []},
            file_path="/tmp/p.json",
            phase="drafted",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )


def test_post_memory_decisions_writes_file(
    postgres_state_store_url: str, tmp_path: Path
) -> None:
    store = make_state_store(postgres_state_store_url)
    client, auth = _client(store, tmp_path)
    resp = client.post(
        "/api/v1/memory/decisions",
        json={"title": "Use RFC 9457", "body": "problem+json", "date": "2026-05-19"},
        headers=auth,
    )
    assert resp.status_code == 201
    assert (tmp_path / "carve" / "decisions.md").read_text().count("Use RFC 9457") == 1


def test_post_runs_replays_response_on_same_idempotency_key(
    postgres_state_store_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same Idempotency-Key → the *middleware* replays the cached 202 (not domain dedup).

    The second request is short-circuited by ``IdempotencyMiddleware`` before it
    reaches ``enqueue_manual`` (``Idempotency-Replayed: true``); domain coalescing
    is proven separately below.
    """
    monkeypatch.setattr("carve.cli.commands.serve.resolve_worker_label", lambda *a, **k: None)
    store = make_state_store(postgres_state_store_url)
    # Make the pipeline runnable (a pipelines row).
    store.repository.create_or_update_pipeline(name="p", description="", pipeline_dir="el/p")
    client, auth = _client(store, tmp_path)

    headers = {**auth, "Idempotency-Key": "run-key"}
    first = client.post("/api/v1/runs", json={"pipeline_name": "p"}, headers=headers)
    second = client.post("/api/v1/runs", json={"pipeline_name": "p"}, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 202
    job_id = first.json()["job_id"]
    assert second.json()["job_id"] == job_id
    assert second.headers.get("Idempotency-Replayed") == "true"  # middleware replay

    # The job actually landed on the queue.
    listed = client.get("/api/v1/jobs", headers=auth)
    assert listed.status_code == 200
    assert any(j["id"] == job_id and j["pipeline"] == "p" for j in listed.json()["items"])


def test_post_runs_domain_coalesces_without_idempotency_key(
    postgres_state_store_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Idempotency-Key → both requests reach ``enqueue_manual``; the ``ON CONFLICT``
    upsert coalesces them onto ONE queued job (domain dedup through the HTTP surface).

    This is the real coalescing proof: with no key the middleware never engages
    (no ``Idempotency-Replayed`` header), so identical job ids can only come from
    the ``ON CONFLICT (pipeline, tenant_id) WHERE status='queued'`` upsert.
    """
    monkeypatch.setattr("carve.cli.commands.serve.resolve_worker_label", lambda *a, **k: None)
    store = make_state_store(postgres_state_store_url)
    store.repository.create_or_update_pipeline(name="p", description="", pipeline_dir="el/p")
    client, auth = _client(store, tmp_path)

    first = client.post("/api/v1/runs", json={"pipeline_name": "p"}, headers=auth)
    second = client.post("/api/v1/runs", json={"pipeline_name": "p"}, headers=auth)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]  # coalesced, not replayed
    assert second.headers.get("Idempotency-Replayed") is None  # middleware did not engage

    # Exactly one queued job for the pipeline.
    items = client.get("/api/v1/jobs", headers=auth).json()["items"]
    queued = [j for j in items if j["pipeline"] == "p" and j["status"] == "queued"]
    assert len(queued) == 1


def test_post_plans_persists_and_returns_201(
    postgres_state_store_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_generate_plan(goal, config, project_dir, *, repository, **kw):  # type: ignore[no-untyped-def]
        _save_plan_kw(repository=repository, plan_id="plan_gen1", config_hash=config.config_hash)
        return SimpleNamespace(
            id="plan_gen1", cost_usd=0.9, tokens_input=10, tokens_output=5
        )

    monkeypatch.setattr("carve.cli.orchestrator.planner.generate_plan", _fake_generate_plan)
    store = make_state_store(postgres_state_store_url)
    client, auth = _client(store, tmp_path)

    resp = client.post("/api/v1/plans", json={"goal": "ingest stripe"}, headers=auth)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "plan_gen1"
    assert body["cost_usd"] == 0.9
    assert store.repository.get_plan("plan_gen1") is not None


def test_post_builds_success_and_drift(
    postgres_state_store_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_state_store(postgres_state_store_url)
    _save_plan(store, "plan_b1")
    client, auth = _client(store, tmp_path)

    # Success path.
    monkeypatch.setattr(
        "carve.cli.orchestrator.builder.build_plan",
        lambda *a, **k: SimpleNamespace(
            build_id="build_1",
            run_id="run_1",
            pipeline_name="p",
            target="dev",
            files_written=["el/p/main.py"],
            cost_usd=0.3,
            success=True,
        ),
    )
    ok = client.post("/api/v1/builds", json={"plan_id": "plan_b1"}, headers=auth)
    assert ok.status_code == 200
    assert ok.json()["build_id"] == "build_1"

    # Drift path round-trips to 409 problem+json with the structured hashes.
    def _drift(*a, **k):  # type: ignore[no-untyped-def]
        from carve.cli.orchestrator.builder import ConfigDriftError

        raise ConfigDriftError("plan_b1", plan_hash="sha256:aaa", current_hash="sha256:bbb")

    monkeypatch.setattr("carve.cli.orchestrator.builder.build_plan", _drift)
    drift = client.post("/api/v1/builds", json={"plan_id": "plan_b1"}, headers=auth)
    assert drift.status_code == 409
    assert drift.json()["type"] == "https://carve.dev/errors/config-drift"
    assert drift.json()["expected_config_hash"] == "sha256:aaa"


def _save_plan_kw(*, repository: object, plan_id: str, config_hash: str) -> None:
    # Keyword-only shim so the fake generate_plan can call it cleanly.
    now = datetime.now(UTC)
    repository.save_plan(  # type: ignore[attr-defined]
        Plan(
            id=plan_id,
            goal="g",
            config_hash=config_hash,
            carve_version="0.0.1",
            task_graph_json={"steps": []},
            file_path="/tmp/p.json",
            phase="drafted",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
