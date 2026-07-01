"""The webhooks repositories — subscriber CRUD + the delivery ledger.

Two repos over three tables:

* :class:`Webhooks` — CRUD over ``webhooks`` (user-declared subscribers). Each
  webhook owns a per-webhook ``hmac_secret`` (returned once at create /
  rotate-secret; stored so the publisher can sign the body).
* :class:`WebhookDeliveries` — the ``webhook_deliveries`` ledger. Fans each
  unprocessed ``events`` row out into one pending delivery per active matching
  webhook (stamping the event ``processed_at`` so the partial
  ``ix_events_unprocessed`` scan stays cheap), then persists the outcome of each
  attempt the publisher makes.

Both mirror the other state-store repos (shared ``sessionmaker``, short sync
transactions). The retry *schedule* + HTTP delivery live in
:mod:`carve.api.webhooks`; these repos only persist state.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa

from carve.core.state.models import Event, Webhook, WebhookDelivery

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


def _utcnow() -> datetime:
    return datetime.now(UTC)


def generate_hmac_secret() -> str:
    """A per-webhook base64-url HMAC secret (256 bits)."""
    return secrets.token_urlsafe(32)


def _matches(event_filters: list[str], kind: str) -> bool:
    """Whether ``kind`` matches a webhook's filters.

    Empty filters (or ``"*"``) subscribe to everything; otherwise the event kind
    must appear verbatim.
    """
    if not event_filters or "*" in event_filters:
        return True
    return kind in event_filters


class Webhooks:
    """CRUD over the ``webhooks`` table."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        url: str,
        event_filters: list[str] | None = None,
        tenant_id: int = 1,
    ) -> Webhook:
        """Create a webhook with a fresh ``hmac_secret``; return the row."""
        webhook = Webhook(
            id="wh_" + uuid.uuid4().hex,
            url=url,
            event_filters=list(event_filters) if event_filters is not None else [],
            hmac_secret=generate_hmac_secret(),
            active=True,
            tenant_id=tenant_id,
            created_at=_utcnow(),
        )
        with self._session_factory() as session:
            session.add(webhook)
            session.commit()
            session.refresh(webhook)
            return webhook

    def get(self, webhook_id: str) -> Webhook | None:
        with self._session_factory() as session:
            return session.get(Webhook, webhook_id)

    def list_all(self, *, tenant_id: int = 1) -> list[Webhook]:
        stmt = (
            sa.select(Webhook)
            .where(Webhook.tenant_id == tenant_id)
            .order_by(Webhook.created_at.desc())
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def list_active(self, *, tenant_id: int = 1) -> list[Webhook]:
        stmt = sa.select(Webhook).where(
            Webhook.tenant_id == tenant_id,
            Webhook.active.is_(True),
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def update(
        self,
        webhook_id: str,
        *,
        url: str | None = None,
        event_filters: list[str] | None = None,
        active: bool | None = None,
    ) -> Webhook | None:
        with self._session_factory() as session:
            webhook = session.get(Webhook, webhook_id)
            if webhook is None:
                return None
            if url is not None:
                webhook.url = url
            if event_filters is not None:
                webhook.event_filters = list(event_filters)
            if active is not None:
                webhook.active = active
            session.commit()
            session.refresh(webhook)
            return webhook

    def rotate_secret(self, webhook_id: str) -> Webhook | None:
        """Generate a new ``hmac_secret`` for a webhook; return the row."""
        with self._session_factory() as session:
            webhook = session.get(Webhook, webhook_id)
            if webhook is None:
                return None
            webhook.hmac_secret = generate_hmac_secret()
            session.commit()
            session.refresh(webhook)
            return webhook

    def delete(self, webhook_id: str) -> bool:
        with self._session_factory() as session:
            webhook = session.get(Webhook, webhook_id)
            if webhook is None:
                return False
            session.delete(webhook)
            session.commit()
            return True


class WebhookDeliveries:
    """The ``webhook_deliveries`` ledger + the event fan-out."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def enqueue_for_new_events(self, *, batch: int = 200, tenant_id: int = 1) -> int:
        """Fan unprocessed ``events`` out into pending deliveries.

        One transaction: claim up to ``batch`` unprocessed events, create one
        ``pending`` delivery per active webhook whose filters match, then stamp
        every claimed event ``processed_at`` (so it leaves the partial
        unprocessed index — even if no webhook matched). Returns the number of
        deliveries enqueued.
        """
        now = _utcnow()
        enqueued = 0
        with self._session_factory() as session:
            events = list(
                session.scalars(
                    sa.select(Event)
                    .where(Event.processed_at.is_(None))
                    .order_by(Event.id.asc())
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                ).all()
            )
            if not events:
                return 0
            active = list(
                session.scalars(
                    sa.select(Webhook).where(
                        Webhook.tenant_id == tenant_id,
                        Webhook.active.is_(True),
                    )
                ).all()
            )
            for event in events:
                for webhook in active:
                    if _matches(webhook.event_filters, event.kind):
                        session.add(
                            WebhookDelivery(
                                id="whd_" + uuid.uuid4().hex,
                                webhook_id=webhook.id,
                                event_id=event.id,
                                retry_count=0,
                                status="pending",
                                next_retry_at=None,
                                created_at=now,
                            )
                        )
                        enqueued += 1
                event.processed_at = now
            session.commit()
        return enqueued

    def pending_or_due_for_retry(
        self, now: datetime | None = None, *, batch: int = 100
    ) -> list[WebhookDelivery]:
        """Return pending deliveries whose ``next_retry_at`` has passed (or is unset)."""
        cutoff = now if now is not None else _utcnow()
        stmt = (
            sa.select(WebhookDelivery)
            .where(
                WebhookDelivery.status == "pending",
                sa.or_(
                    WebhookDelivery.next_retry_at.is_(None),
                    WebhookDelivery.next_retry_at <= cutoff,
                ),
            )
            .order_by(WebhookDelivery.created_at.asc())
            .limit(batch)
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def get(self, delivery_id: str) -> WebhookDelivery | None:
        with self._session_factory() as session:
            return session.get(WebhookDelivery, delivery_id)

    def list_for_webhook(self, webhook_id: str) -> list[WebhookDelivery]:
        stmt = (
            sa.select(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == webhook_id)
            .order_by(WebhookDelivery.created_at.asc())
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def mark_delivered(self, delivery_id: str, *, response_status: int) -> None:
        self._finish(delivery_id, status="delivered", response_status=response_status)

    def mark_retry(
        self,
        delivery_id: str,
        *,
        retry_count: int,
        next_retry_at: datetime,
        response_status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            delivery = session.get(WebhookDelivery, delivery_id)
            if delivery is None:
                return
            delivery.status = "pending"
            delivery.retry_count = retry_count
            delivery.next_retry_at = next_retry_at
            delivery.attempted_at = _utcnow()
            delivery.response_status = response_status
            delivery.response_body = _truncate(response_body)
            session.commit()

    def mark_abandoned(
        self,
        delivery_id: str,
        *,
        response_status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        self._finish(
            delivery_id,
            status="abandoned",
            response_status=response_status,
            response_body=response_body,
        )

    def _finish(
        self,
        delivery_id: str,
        *,
        status: str,
        response_status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            delivery = session.get(WebhookDelivery, delivery_id)
            if delivery is None:
                return
            delivery.status = status
            delivery.attempted_at = _utcnow()
            delivery.next_retry_at = None
            if response_status is not None:
                delivery.response_status = response_status
            if response_body is not None:
                delivery.response_body = _truncate(response_body)
            session.commit()


def _truncate(body: str | None, *, limit: int = 2000) -> str | None:
    """Bound a stored response body so a chatty subscriber can't bloat the row."""
    if body is None:
        return None
    return body if len(body) <= limit else body[:limit]


__all__ = ["WebhookDeliveries", "Webhooks", "generate_hmac_secret"]
