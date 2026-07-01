"""Tokens repo: create/find_by_token/touch_last_used/revoke against Postgres."""

from __future__ import annotations

import pytest

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.tokens import Tokens, generate_token, hash_token, verify_token


@pytest.fixture
def tokens(postgres_state_store_url: str) -> Tokens:
    config = Config(
        project=ProjectConfig(name="tokens-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    return Tokens(create_session_factory(engine))


def test_hash_and_verify_round_trip() -> None:
    hashed = hash_token("carve_pat_secret")
    assert hashed != "carve_pat_secret"  # only the hash is stored
    assert verify_token(hashed, "carve_pat_secret") is True
    assert verify_token(hashed, "wrong") is False


def test_generate_token_has_lookup_and_secret() -> None:
    lookup, plaintext = generate_token()
    assert plaintext.startswith("carve_pat_")
    # Plaintext is carve_pat_<lookup>.<secret> — one separator, both parts present.
    assert plaintext == f"carve_pat_{lookup}.{plaintext.split('.', 1)[1]}"
    assert plaintext.count(".") == 1
    assert plaintext.split(".", 1)[1]  # non-empty secret


def test_find_by_token_rejects_malformed_tokens(tokens: Tokens) -> None:
    tokens.create(scopes=["*"])
    # No prefix, and no separator → parse fails, rejected without a DB hit.
    assert tokens.find_by_token("not_a_token") is None
    assert tokens.find_by_token("carve_pat_nolookupsep") is None


def test_find_resolves_the_right_token_among_many(tokens: Tokens) -> None:
    minted = [tokens.create(scopes=["*"]) for _ in range(5)]
    for token_id, plaintext in minted:
        identity = tokens.find_by_token(plaintext)
        assert identity is not None
        assert identity.token_id == token_id


def test_create_returns_plaintext_and_find_resolves_identity(tokens: Tokens) -> None:
    token_id, plaintext = tokens.create(scopes=["*"])
    assert plaintext.startswith("carve_pat_")

    identity = tokens.find_by_token(plaintext)
    assert identity is not None
    assert identity.token_id == token_id
    assert identity.scopes == ["*"]
    assert identity.user_id == 1
    assert identity.tenant_id == 1


def test_find_by_token_rejects_unknown(tokens: Tokens) -> None:
    tokens.create(scopes=["*"])
    assert tokens.find_by_token("carve_pat_not_a_real_token") is None


def test_revoked_token_no_longer_authenticates(tokens: Tokens) -> None:
    token_id, plaintext = tokens.create(scopes=["*"])
    assert tokens.find_by_token(plaintext) is not None
    assert tokens.revoke(token_id) is True
    assert tokens.find_by_token(plaintext) is None


def test_touch_last_used_sets_timestamp(tokens: Tokens) -> None:
    token_id, _ = tokens.create(scopes=["*"])
    tokens.touch_last_used(token_id)
    row = next(t for t in tokens.list_all(include_revoked=True) if t.id == token_id)
    assert row.last_used_at is not None


def test_has_active_default_tracks_default_bootstrap(tokens: Tokens) -> None:
    assert tokens.has_active_default() is False
    tokens.create(scopes=["*"], is_default=True)
    assert tokens.has_active_default() is True
