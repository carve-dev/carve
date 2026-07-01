"""IdempotencyKeys repo: lookup/store/delete_expired against Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.idempotency import IdempotencyKeys


@pytest.fixture
def repo(postgres_state_store_url: str) -> IdempotencyKeys:
    config = Config(
        project=ProjectConfig(name="idem-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return IdempotencyKeys(create_session_factory(engine))


def test_store_then_lookup_round_trips(repo: IdempotencyKeys) -> None:
    repo.store(
        tenant_id=1,
        user_id=1,
        key="k1",
        request_hash="h1",
        response_status=201,
        response_body={"id": "x"},
        response_headers={"content-type": "application/json"},
    )
    cached = repo.lookup(1, 1, "k1")
    assert cached is not None
    assert cached.request_hash == "h1"
    assert cached.response_status == 201
    assert cached.response_body == {"id": "x"}


def test_lookup_absent_is_none(repo: IdempotencyKeys) -> None:
    assert repo.lookup(1, 1, "missing") is None


def test_keys_are_scoped_per_user(repo: IdempotencyKeys) -> None:
    repo.store(
        tenant_id=1,
        user_id=1,
        key="shared",
        request_hash="h",
        response_status=200,
        response_body={},
        response_headers={},
    )
    # A different user's identical key does not collide.
    assert repo.lookup(1, 2, "shared") is None


def test_expired_row_is_not_returned_and_is_gc_able(repo: IdempotencyKeys) -> None:
    past = datetime.now(UTC) - timedelta(hours=48)
    repo.store(
        tenant_id=1,
        user_id=1,
        key="old",
        request_hash="h",
        response_status=200,
        response_body={},
        response_headers={},
        now=past,  # expires 24h after → still in the past
    )
    # Expired: lookup treats it as absent (fresh execution).
    assert repo.lookup(1, 1, "old") is None
    # And the hourly GC removes it.
    assert repo.delete_expired() == 1
    assert repo.delete_expired() == 0
