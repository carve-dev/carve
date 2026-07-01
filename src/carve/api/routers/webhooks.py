"""``/api/v1/webhooks`` — subscriber CRUD + secret rotation.

``POST`` / ``rotate-secret`` return the plaintext ``hmac_secret`` once; ``GET`` /
``PATCH`` responses omit it (the subscriber recorded it at create time).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import get_state_store
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
    from carve.core.state.store import StateStore

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class CreateWebhookBody(BaseModel):
    url: str
    event_filters: list[str] | None = None


class UpdateWebhookBody(BaseModel):
    url: str | None = None
    event_filters: list[str] | None = None
    active: bool | None = None


class WebhookOut(BaseModel):
    """Webhook metadata (no secret)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    url: str
    event_filters: list[str]
    active: bool
    created_at: datetime


class WebhookWithSecret(WebhookOut):
    """Create / rotate-secret response — includes the plaintext ``hmac_secret`` once."""

    hmac_secret: str


@router.get("", response_model=list[WebhookOut])
def list_webhooks(
    state_store: StateStore = Depends(get_state_store),
) -> list[WebhookOut]:
    """List webhooks (secret omitted)."""
    return [WebhookOut.model_validate(w) for w in state_store.webhooks.list_all()]


@router.post("", response_model=WebhookWithSecret, status_code=201)
def create_webhook(
    body: CreateWebhookBody,
    state_store: StateStore = Depends(get_state_store),
) -> WebhookWithSecret:
    """Create a webhook; the ``hmac_secret`` is returned once."""
    webhook = state_store.webhooks.create(url=body.url, event_filters=body.event_filters)
    return WebhookWithSecret.model_validate(webhook)


@router.patch("/{webhook_id}", response_model=WebhookOut)
def update_webhook(
    webhook_id: str,
    body: UpdateWebhookBody,
    state_store: StateStore = Depends(get_state_store),
) -> WebhookOut:
    """Update a webhook's url / filters / active flag."""
    webhook = state_store.webhooks.update(
        webhook_id,
        url=body.url,
        event_filters=body.event_filters,
        active=body.active,
    )
    if webhook is None:
        raise ResourceNotFound(f"Webhook {webhook_id!r} not found.")
    return WebhookOut.model_validate(webhook)


@router.delete("/{webhook_id}", status_code=204)
def delete_webhook(
    webhook_id: str,
    state_store: StateStore = Depends(get_state_store),
) -> Response:
    """Delete a webhook."""
    if not state_store.webhooks.delete(webhook_id):
        raise ResourceNotFound(f"Webhook {webhook_id!r} not found.")
    return Response(status_code=204)


@router.post("/{webhook_id}/rotate-secret", response_model=WebhookWithSecret)
def rotate_secret(
    webhook_id: str,
    state_store: StateStore = Depends(get_state_store),
) -> WebhookWithSecret:
    """Generate a new ``hmac_secret``; returned once."""
    webhook = state_store.webhooks.rotate_secret(webhook_id)
    if webhook is None:
        raise ResourceNotFound(f"Webhook {webhook_id!r} not found.")
    return WebhookWithSecret.model_validate(webhook)


__all__ = ["router"]
