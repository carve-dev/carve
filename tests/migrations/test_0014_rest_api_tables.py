"""Migration 0014 — the REST-API tables — up/down round-trip against Postgres.

Asserts the four tables (``tokens``/``idempotency_keys``/``webhooks``/
``webhook_deliveries``), their indexes, and the two ``webhook_deliveries`` FKs
land at head, and that downgrading to 0013 drops them cleanly. Skips when Docker
is absent (via the ``postgres_state_store_url`` fixture).
"""

from __future__ import annotations

from alembic import command as alembic_command
from sqlalchemy import inspect, text

from carve.core.config.schema import Config, ModelsConfig, ProjectConfig, ServerConfig
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    _alembic_config,
    create_engine_from_config,
    initialize_database,
)


def _make_config(state_store_url: str) -> Config:
    return Config(
        project=ProjectConfig(name="migration-0014-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=state_store_url),
    )


def test_0014_creates_rest_api_tables(postgres_state_store_url: str) -> None:
    engine = create_engine_from_config(_make_config(postgres_state_store_url))
    try:
        initialize_database(engine)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"tokens", "idempotency_keys", "webhooks", "webhook_deliveries"}.issubset(tables)

        # idempotency_keys composite PK.
        pk = inspector.get_pk_constraint("idempotency_keys")
        assert set(pk["constrained_columns"]) == {"tenant_id", "user_id", "key"}
        idem_indexes = {ix["name"] for ix in inspector.get_indexes("idempotency_keys")}
        assert "ix_idempotency_keys_expires_at" in idem_indexes

        # tokens indexes (hash + lookup + partial one-default).
        token_indexes = {ix["name"] for ix in inspector.get_indexes("tokens")}
        assert {
            "ix_tokens_token_hash",
            "ix_tokens_lookup_id",
            "ix_tokens_one_default",
        }.issubset(token_indexes)

        # webhook_deliveries FKs → webhooks(id) and events(id).
        fk_targets = {
            fk["referred_table"] for fk in inspector.get_foreign_keys("webhook_deliveries")
        }
        assert {"webhooks", "events"}.issubset(fk_targets)
        wd_indexes = {ix["name"] for ix in inspector.get_indexes("webhook_deliveries")}
        assert "ix_webhook_deliveries_due" in wd_indexes

        with engine.connect() as conn:
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert head == "0014_rest_api_tables"
    finally:
        engine.dispose()


def test_0014_one_default_token_partial_index_enforced(postgres_state_store_url: str) -> None:
    """The partial unique index allows at most one active default token."""
    from sqlalchemy.exc import IntegrityError

    engine = create_engine_from_config(_make_config(postgres_state_store_url))
    try:
        initialize_database(engine)
        insert = text(
            "INSERT INTO tokens (id, token_hash, lookup_id, scopes, is_default, created_at) "
            "VALUES (:id, :h, :lk, '[\"*\"]'::jsonb, true, now())"
        )
        with engine.begin() as conn:
            conn.execute(insert, {"id": "tok_a", "h": "hash_a", "lk": "lookup_a"})
        with engine.begin() as conn:
            try:
                conn.execute(insert, {"id": "tok_b", "h": "hash_b", "lk": "lookup_b"})
                raise AssertionError("expected a second default token to violate the index")
            except IntegrityError:
                pass
    finally:
        engine.dispose()


def test_0014_downgrade_drops_rest_api_tables(postgres_state_store_url: str) -> None:
    engine = create_engine_from_config(_make_config(postgres_state_store_url))
    try:
        initialize_database(engine)
        assert {"tokens", "webhooks", "webhook_deliveries", "idempotency_keys"}.issubset(
            set(inspect(engine).get_table_names())
        )

        cfg = _alembic_config(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.downgrade(cfg, "0013_runtime_worker_placement")

        remaining = set(inspect(engine).get_table_names())
        for table in ("tokens", "webhooks", "webhook_deliveries", "idempotency_keys"):
            assert table not in remaining
        # 0013's schema is intact (events stays — webhook_deliveries FK'd it).
        assert {"jobs", "events", "schedules"}.issubset(remaining)

        with engine.connect() as conn:
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert head == "0013_runtime_worker_placement"
    finally:
        engine.dispose()
