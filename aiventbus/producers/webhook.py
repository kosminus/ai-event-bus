"""Webhook producer — turns incoming HTTP POST requests into bus events.

Exposes an endpoint on the main FastAPI app at ``/api/v1/webhook/{topic_path}``.
When enabled, incoming POSTs are published as events.  When disabled, returns 503.

Authentication is optional: set ``webhook_secret`` in config to require a
``Authorization: Bearer <secret>`` header.  GitHub-style ``X-Hub-Signature-256``
HMAC verification is also supported when a secret is configured.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


class WebhookProducer(BaseProducer):
    """Receives HTTP webhooks and publishes them as bus events.

    This producer doesn't run a background task — it exposes a
    ``receive()`` method called by the FastAPI webhook router.
    """

    def __init__(
        self,
        bus: EventBus,
        secret: str | None = None,
        default_priority: str = "medium",
    ):
        self.bus = bus
        self.secret = secret
        self.default_priority = default_priority
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("Webhook producer started")

    async def stop(self) -> None:
        self._running = False
        logger.info("Webhook producer stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def verify_bearer(self, auth_header: str | None) -> bool:
        """Check Bearer token if a secret is configured."""
        if not self.secret:
            return True
        if not auth_header:
            return False
        scheme, _, token = auth_header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(token, self.secret)

    def verify_signature(self, body: bytes, signature_header: str | None) -> bool:
        """Verify GitHub-style X-Hub-Signature-256 HMAC."""
        if not self.secret or not signature_header:
            return False
        expected = "sha256=" + hmac.new(
            self.secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header)

    async def receive(
        self,
        topic_path: str,
        payload: dict[str, Any],
        source: str | None = None,
        priority: str | None = None,
        dedupe_key: str | None = None,
    ) -> str:
        """Publish an incoming webhook as a bus event. Returns the event ID."""
        topic = f"webhook.{topic_path.replace('/', '.')}"
        pri = Priority(priority) if priority and priority in Priority.__members__ else Priority(self.default_priority)

        event = await self.bus.publish(
            EventCreate(
                topic=topic,
                payload=payload,
                priority=pri,
                dedupe_key=dedupe_key,
                source=source or f"webhook:{topic_path}",
            ),
            producer_id="producer_webhook",
        )
        logger.debug("Webhook event published: %s (topic=%s)", event.id, topic)
        return event.id
