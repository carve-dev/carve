"""Add the REST-API substrate: idempotency_keys, tokens, webhooks, webhook_deliveries.

Revision ID: 0014_rest_api_tables
Revises: 0013_runtime_worker_placement
Create Date: 2026-07-01

Increment 5's **rest-api slice** wires a FastAPI surface over the already-shipped
services. Four tables carry the cross-cutting middleware + webhook publisher:

* ``tokens`` — API bearer tokens. The plaintext is ``carve_pat_<lookup>.<secret>``;
  only the argon2 ``token_hash`` is stored (plaintext returned once at mint). The
  indexed, unique ``lookup_id`` (the ``<lookup>`` segment) bounds authentication to
  a **single** argon2 verify per request (``WHERE lookup_id = :lookup`` first),
  instead of verifying against every active token. ``scopes`` is ``["*"]`` for the
  OSS default token. ``is_default`` marks the bootstrapped default token; the
  partial unique ``ix_tokens_one_default`` keeps at most one so
  ``ensure_default_token`` is idempotent across ``carve serve`` restarts.
* ``idempotency_keys`` — the ``Idempotency-Key`` replay cache. PK is
  ``(tenant_id, user_id, key)`` so two users' keys never collide; the cached
  response (status/body/headers) replays within 24h, and
  ``ix_idempotency_keys_expires_at`` backs the hourly GC.
* ``webhooks`` — user-declared subscribers (``url``/``event_filters``/per-webhook
  ``hmac_secret``).
* ``webhook_deliveries`` — one row per (event x matching webhook). FKs
  ``webhooks(id)`` and ``events(id)`` (``events`` exists since 0010). The
  publisher loop drains ``status='pending'`` rows on the documented retry
  schedule; ``ix_webhook_deliveries_due`` (partial, ``WHERE status='pending'``)
  keeps the due-scan cheap.

These four tables were assumed-present by the rest-api capability spec, which
misattributed ``tokens``/``webhooks``/``webhook_deliveries`` to "spec 07's
migration" — but the runtime slices never created them. This migration **absorbs**
the three assumed tables and **creates** the one the spec defines
(``idempotency_keys``); none is owned by a prior capability, so absorbing them
here is in-scope.

Downgrade drops the indexes then the tables in reverse-FK order
(``webhook_deliveries`` before ``webhooks``/``events``), restoring 0013's schema
exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0014_rest_api_tables"
down_revision: str | None = "0013_runtime_worker_placement"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the four REST-API tables + their indexes."""
    # tokens ---------------------------------------------------------------
    op.create_table(
        "tokens",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        # The argon2 hash of the plaintext bearer token; the plaintext is never
        # stored (returned once at mint). Salted, so it is not an equality lookup
        # key — the repo narrows by ``lookup_id`` then argon2-verifies the one row.
        sa.Column("token_hash", sa.String(), nullable=False),
        # The non-secret ``<lookup>`` segment of the plaintext token, indexed so
        # authentication is one argon2 verify (not one per active token).
        sa.Column("lookup_id", sa.String(), nullable=False),
        sa.Column("scopes", JSONB, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, server_default="1"),
        # Marks the OSS default token so ``ensure_default_token`` is idempotent.
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_tokens_token_hash", "tokens", ["token_hash"])
    # The authentication lookup index: narrow to the one candidate row by its
    # non-secret ``<lookup>`` segment before the single argon2 verify.
    op.create_index("ix_tokens_lookup_id", "tokens", ["lookup_id"], unique=True)
    # At most one default token (bootstrap idempotency). Partial so revoked /
    # non-default rows never collide.
    op.create_index(
        "ix_tokens_one_default",
        "tokens",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default AND revoked_at IS NULL"),
    )

    # idempotency_keys -----------------------------------------------------
    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", JSONB, nullable=False),
        sa.Column("response_headers", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "key"),
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])

    # webhooks -------------------------------------------------------------
    op.create_table(
        "webhooks",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("event_filters", JSONB, nullable=False),
        # Per-webhook HMAC secret (base64-url random). Returned once at create /
        # rotate-secret; used to sign the delivery body.
        sa.Column("hmac_secret", sa.String(), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # webhook_deliveries ---------------------------------------------------
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "webhook_id",
            sa.String(),
            sa.ForeignKey("webhooks.id", name="fk_webhook_deliveries_webhook_id"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("events.id", name="fk_webhook_deliveries_event_id"),
            nullable=False,
        ),
        sa.Column(
            "attempted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.String(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        # pending | delivered | failed | abandoned
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # The due-scan the publisher rides: pending rows whose next_retry_at has
    # passed (or is unset). Partial so delivered/abandoned rows never enter it.
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["next_retry_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_webhook_deliveries_webhook_id", "webhook_deliveries", ["webhook_id"])


def downgrade() -> None:
    """Drop the four REST-API tables (indexes first, reverse-FK order)."""
    op.drop_index("ix_webhook_deliveries_webhook_id", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")

    op.drop_table("webhooks")

    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")

    op.drop_index("ix_tokens_one_default", table_name="tokens")
    op.drop_index("ix_tokens_lookup_id", table_name="tokens")
    op.drop_index("ix_tokens_token_hash", table_name="tokens")
    op.drop_table("tokens")


__all__ = ["downgrade", "upgrade"]
