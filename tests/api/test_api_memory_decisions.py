"""POST /memory/decisions — appends; duplicate → 409; multiline/empty title → 400."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from carve.api.main import create_app
from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import Config
from tests.api.conftest import GOOD_TOKEN

_AUTH = {"Authorization": f"Bearer {GOOD_TOKEN}"}


@pytest.fixture
def client(fake_store: MagicMock, api_config: Config, tmp_path: Path) -> TestClient:
    # Real MemoryWriter (no agent); writes under a tmp project root.
    return TestClient(
        create_app(fake_store, api_config, project_paths=ProjectPaths.from_root(tmp_path)),
        raise_server_exceptions=False,
    )


def test_append_decision_writes_and_returns_201(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/v1/memory/decisions",
        json={"title": "Adopt cursor pagination", "body": "Opaque cursors.", "date": "2026-05-19"},
        headers=_AUTH,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "decisions"
    written = Path(body["path"])
    assert written.name == "decisions.md"
    assert "Adopt cursor pagination" in written.read_text()


def test_duplicate_decision_is_409(client: TestClient) -> None:
    payload = {"title": "Same call", "body": "b", "date": "2026-05-19"}
    assert client.post("/api/v1/memory/decisions", json=payload, headers=_AUTH).status_code == 201
    dup = client.post("/api/v1/memory/decisions", json=payload, headers=_AUTH)
    assert dup.status_code == 409
    assert dup.json()["type"] == "https://carve.dev/errors/decision-exists"


def test_force_allows_duplicate(client: TestClient) -> None:
    payload = {"title": "Forced", "body": "b", "date": "2026-05-19"}
    client.post("/api/v1/memory/decisions", json=payload, headers=_AUTH)
    forced = client.post(
        "/api/v1/memory/decisions", json={**payload, "force": True}, headers=_AUTH
    )
    assert forced.status_code == 201


def test_multiline_title_is_400(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/memory/decisions",
        json={"title": "line one\nforged ## heading", "body": "b"},
        headers=_AUTH,
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "https://carve.dev/errors/bad-request"


def test_empty_title_is_400(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/memory/decisions", json={"title": "   ", "body": "b"}, headers=_AUTH
    )
    assert resp.status_code == 400
