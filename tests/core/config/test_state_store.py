"""Tests for ``resolve_state_store_url`` ``DATABASE_URL`` env precedence.

These tests pin the four-step precedence the resolver implements:

1. ``state_store.url`` (when not the module default)
2. ``DATABASE_URL`` env var (truthy; empty string treated as unset)
3. ``server.state_store`` legacy alias (when set and not default)
4. ``DEFAULT_STATE_STORE_URL``

Pure-Python tests — no Postgres or testcontainers; the resolver is a
plain function over a pydantic ``Config``.
"""

from __future__ import annotations

import pytest

from carve.core.config.schema import (
    Config,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.config.state_store import (
    DEFAULT_STATE_STORE_URL,
    StateStoreConfig,
    resolve_state_store_url,
)

ENV_URL = "postgresql+psycopg://env:env@db.env.example.com/carve"
EXPLICIT_URL = "postgresql+psycopg://explicit:explicit@db.explicit.example.com/carve"
LEGACY_URL = "postgresql+psycopg://legacy:legacy@db.legacy.example.com/carve"


def _make_config(
    *,
    state_store_url: str | None = None,
    legacy_state_store: str | None = None,
) -> Config:
    """Build a minimal `Config` for resolver tests.

    Mirrors `_make_config(state_db=...)` helpers elsewhere in the suite
    but exposes both knobs the resolver cares about: the v0.1 idiom
    (``state_store.url``) and the legacy M1 alias (``server.state_store``).
    """
    state_store = (
        StateStoreConfig(url=state_store_url) if state_store_url is not None else StateStoreConfig()
    )
    server = (
        ServerConfig(state_store=legacy_state_store)
        if legacy_state_store is not None
        else ServerConfig()
    )
    return Config(
        project=ProjectConfig(name="test-project"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=server,
        state_store=state_store,
    )


def test_explicit_state_store_url_wins_over_database_url_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-default ``state_store.url`` from ``runtime.toml`` is intentional
    configuration and must beat any ambient ``DATABASE_URL`` env var.
    """
    monkeypatch.setenv("DATABASE_URL", ENV_URL)
    config = _make_config(state_store_url=EXPLICIT_URL)

    assert resolve_state_store_url(config) == EXPLICIT_URL


def test_database_url_env_wins_when_state_store_url_is_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bootstrap-Config case: ``carve init`` constructs a Config whose
    ``state_store.url`` is the module default. When ``DATABASE_URL`` is
    exported, the resolver must route to it — that's the gap this spec
    closes.
    """
    monkeypatch.setenv("DATABASE_URL", ENV_URL)
    config = _make_config()  # state_store.url == DEFAULT_STATE_STORE_URL

    assert resolve_state_store_url(config) == ENV_URL


@pytest.mark.parametrize(
    ("env_value", "legacy_value", "explicit_value", "expected"),
    [
        # No env, no legacy → default
        (None, None, None, DEFAULT_STATE_STORE_URL),
        # No env, legacy set → legacy
        (None, LEGACY_URL, None, LEGACY_URL),
        # Env set, legacy also set, state_store.url is default → env beats legacy
        (ENV_URL, LEGACY_URL, None, ENV_URL),
        # Env unset, legacy unset, state_store.url is non-default → state_store.url
        (None, None, EXPLICIT_URL, EXPLICIT_URL),
    ],
    ids=[
        "no-env-no-legacy-default",
        "no-env-legacy-set",
        "env-beats-legacy",
        "explicit-wins-alone",
    ],
)
def test_falls_through_to_legacy_then_default(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    legacy_value: str | None,
    explicit_value: str | None,
    expected: str,
) -> None:
    """Cover the remaining precedence rungs in one parameterized sweep."""
    if env_value is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", env_value)

    config = _make_config(
        state_store_url=explicit_value,
        legacy_state_store=legacy_value,
    )

    assert resolve_state_store_url(config) == expected
