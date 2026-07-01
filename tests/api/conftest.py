"""Shared fixtures for the REST-API unit tests (no Docker/Postgres needed).

The state store is a ``MagicMock`` with the few methods the auth + idempotency
middleware actually call wired to in-memory behavior, so these tests exercise the
FastAPI app + middleware without a live database.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig
from carve.core.state.idempotency import CachedResponse
from carve.core.state.tokens import Identity

GOOD_TOKEN = "carve_pat_good_token_value"


class InMemoryIdempotency:
    """A tiny in-memory stand-in for the ``IdempotencyKeys`` repo."""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, int, str], CachedResponse] = {}

    def lookup(self, tenant_id: int, user_id: int, key: str) -> CachedResponse | None:
        return self.rows.get((tenant_id, user_id, key))

    def store(
        self,
        *,
        tenant_id: int,
        user_id: int,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: dict[str, Any],
        response_headers: dict[str, str],
    ) -> None:
        self.rows[(tenant_id, user_id, key)] = CachedResponse(
            request_hash=request_hash,
            response_status=response_status,
            response_body=response_body,
            response_headers=response_headers,
        )


@pytest.fixture
def identity() -> Identity:
    return Identity(user_id=1, tenant_id=1, token_id="tok_test", scopes=["*"])


@pytest.fixture
def api_config() -> Config:
    return Config(
        project=ProjectConfig(name="api-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
    )


@pytest.fixture
def fake_store(identity: Identity) -> MagicMock:
    """A MagicMock StateStore wired for auth + idempotency."""
    store = MagicMock()
    store.tokens.find_by_token.side_effect = lambda plain: (
        identity if plain == GOOD_TOKEN else None
    )
    store.tokens.touch_last_used.return_value = None
    store.tokens.list_all.return_value = []
    store.idempotency_keys = InMemoryIdempotency()
    return store
