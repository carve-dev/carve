"""Webhook publisher — deliver durable events to subscribers, signed + retried.

The loop fans unprocessed ``events`` out into pending ``webhook_deliveries`` and
drains due rows: POST the event JSON to ``webhook.url`` with an
``X-Carve-Signature: sha256=<HMAC-SHA256(hmac_secret, body)>`` over the exact
bytes sent, a 5s timeout, and the documented retry schedule
(30s/1m/5m/15m/1h/3h — six delays), marking ``abandoned`` once the schedule is
exhausted.

**SSRF guard.** ``webhook.url`` is user-controlled and the server makes the
outbound POST, so before *every* attempt the host is resolved and each resolved
IP is checked against loopback / link-local / private / cloud-metadata /
ULA ranges (DNS-rebinding-aware — the literal is not trusted). Delivery to a
blocked address is refused unless ``[api] allow_private_webhook_ips`` opts in.
Mirrors the path-confinement discipline in ``cli/commands/serve.py`` /
``runtime/skills/pipeline_inspect.py``. Secrets (``hmac_secret``) are never
logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from starlette.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from carve.core.state.models import WebhookDelivery
    from carve.core.state.store import StateStore

logger = logging.getLogger(__name__)

#: Per-attempt HTTP timeout.
DELIVERY_TIMEOUT_S = 5.0
#: Retry back-off (seconds): 30s, 1m, 5m, 15m, 1h, 3h — exactly six delays. The
#: schedule is exhausted after these, at which point the delivery is abandoned.
RETRY_SCHEDULE_S: tuple[int, ...] = (30, 60, 300, 900, 3600, 10800)
#: Default publisher poll cadence.
DEFAULT_INTERVAL_S = 10.0


class WebhookUrlBlocked(Exception):
    """Raised when a webhook URL resolves to a disallowed (SSRF-risky) address."""


def _next_delay_s(retry_count: int) -> int | None:
    """The delay before the next attempt, or ``None`` when the schedule is spent."""
    if retry_count < len(RETRY_SCHEDULE_S):
        return RETRY_SCHEDULE_S[retry_count]
    return None


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an IP is in a range the publisher must never POST to."""
    return (
        ip.is_loopback
        or ip.is_link_local  # 169.254/16 (incl. metadata 169.254.169.254), fe80::/10
        or ip.is_private  # 10/8, 172.16/12, 192.168/16, fc00::/7, ::1, ...
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_safe_webhook_url(url: str, *, allow_private: bool = False) -> None:
    """Validate ``url`` against the SSRF blocklist. Raises :class:`WebhookUrlBlocked`.

    Requires an ``http``/``https`` scheme, resolves the host, and rejects the
    delivery if *any* resolved address is loopback/link-local/private/metadata/
    reserved/multicast — unless ``allow_private`` opts in.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise WebhookUrlBlocked(f"unsupported scheme {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise WebhookUrlBlocked("missing host")
    if allow_private:
        return

    # A literal IP is checked directly; a hostname is resolved and every returned
    # address is checked (DNS-rebinding-aware — don't trust the literal name).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(literal):
            raise WebhookUrlBlocked(f"address {host} is in a blocked range")
        return

    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise WebhookUrlBlocked(f"could not resolve host {host!r}") from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_blocked(ip):
            raise WebhookUrlBlocked(f"host {host!r} resolves to blocked address {addr}")


def sign_body(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` HMAC signature over the exact body bytes."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _event_body(event: Any) -> bytes:
    """Serialize an ``events`` row to the delivery payload bytes."""
    payload = {
        "event": event.kind,
        "timestamp": event.occurred_at.isoformat() if event.occurred_at else None,
        "data": event.payload,
    }
    return json.dumps(payload).encode("utf-8")


async def attempt_delivery(
    client: httpx.AsyncClient,
    state_store: StateStore,
    delivery: WebhookDelivery,
    *,
    allow_private_ips: bool = False,
) -> None:
    """Attempt one delivery; persist the outcome (delivered / retry / abandoned)."""
    webhooks = state_store.webhooks
    deliveries = state_store.webhook_deliveries

    webhook = await run_in_threadpool(webhooks.get, delivery.webhook_id)
    if webhook is None or not webhook.active:
        # Subscriber was deleted or disabled: stop trying.
        await run_in_threadpool(deliveries.mark_abandoned, delivery.id)
        return

    event = await run_in_threadpool(state_store.events.get, delivery.event_id)
    if event is None:  # pragma: no cover - event rows are never deleted mid-flight
        await run_in_threadpool(deliveries.mark_abandoned, delivery.id)
        return

    body = _event_body(event)
    headers = {
        "Content-Type": "application/json",
        "X-Carve-Signature": sign_body(webhook.hmac_secret, body),
        "X-Carve-Event": event.kind,
        "X-Carve-Delivery-Id": delivery.id,
    }

    try:
        # ``assert_safe_webhook_url`` resolves DNS (blocking); offload it so a
        # slow/hostile host can't freeze uvicorn's shared event loop.
        # NOTE(rest-api): see issue — SSRF TOCTOU (guard resolves, httpx
        # re-resolves) is a tracked deferral; redirects are off + we re-check
        # before every attempt.
        await run_in_threadpool(
            assert_safe_webhook_url, webhook.url, allow_private=allow_private_ips
        )
    except WebhookUrlBlocked as exc:
        logger.warning("webhook %s URL blocked by SSRF guard: %s", webhook.id, exc)
        await run_in_threadpool(
            deliveries.mark_abandoned,
            delivery.id,
            response_body=f"blocked: {exc}",
        )
        return

    try:
        response = await client.post(
            webhook.url,
            content=body,
            headers=headers,
            timeout=DELIVERY_TIMEOUT_S,
        )
        status = response.status_code
        response_body = response.text
    except httpx.HTTPError as exc:
        status = None
        response_body = f"error: {exc}"

    if status is not None and 200 <= status < 300:
        await run_in_threadpool(deliveries.mark_delivered, delivery.id, response_status=status)
        return

    delay = _next_delay_s(delivery.retry_count)
    if delay is None:
        await run_in_threadpool(
            deliveries.mark_abandoned,
            delivery.id,
            response_status=status,
            response_body=response_body,
        )
        return

    from datetime import UTC, datetime, timedelta

    next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
    await run_in_threadpool(
        deliveries.mark_retry,
        delivery.id,
        retry_count=delivery.retry_count + 1,
        next_retry_at=next_retry_at,
        response_status=status,
        response_body=response_body,
    )


async def _drain_once(
    client: httpx.AsyncClient,
    state_store: StateStore,
    *,
    allow_private_ips: bool,
) -> None:
    """One publisher pass: fan out new events, then attempt every due delivery."""
    await run_in_threadpool(state_store.webhook_deliveries.enqueue_for_new_events)
    due = await run_in_threadpool(state_store.webhook_deliveries.pending_or_due_for_retry)
    for delivery in due:
        await attempt_delivery(
            client, state_store, delivery, allow_private_ips=allow_private_ips
        )


async def webhook_publisher_loop(
    state_store: StateStore,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    allow_private_ips: bool = False,
    shutdown: asyncio.Event | None = None,
) -> None:
    """Run the publisher until ``shutdown`` is set (or forever if ``None``).

    Each pass is best-effort: a per-pass error is logged and the loop continues
    (webhook delivery must never crash ``carve serve``). Stops between passes.
    """
    async with httpx.AsyncClient() as client:
        while shutdown is None or not shutdown.is_set():
            try:
                await _drain_once(client, state_store, allow_private_ips=allow_private_ips)
            except Exception:
                logger.warning("webhook publisher pass failed; continuing", exc_info=True)
            if shutdown is None:
                await asyncio.sleep(interval_s)
            else:
                with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(shutdown.wait(), timeout=interval_s)


__all__ = [
    "DELIVERY_TIMEOUT_S",
    "RETRY_SCHEDULE_S",
    "WebhookUrlBlocked",
    "assert_safe_webhook_url",
    "attempt_delivery",
    "sign_body",
    "webhook_publisher_loop",
]
