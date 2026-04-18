"""Core Event Bus — the central nervous system.

All events flow through here: ingest → dedupe → persist → route → assign → broadcast.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine

from uuid import uuid4

from aiventbus.config import AppConfig
from aiventbus.models import Event, EventCreate, EventStatus
from aiventbus.storage.repositories import (
    AssignmentRepository,
    EventRepository,
)
from aiventbus.telemetry import (
    EVENT_PUBLISH_DURATION_SECONDS,
    record_chain_limit,
    record_event_deduped,
    record_event_published,
    record_producer_emit,
    record_system_event,
)

logger = logging.getLogger(__name__)


class WebSocketHub:
    """Manages WebSocket connections and channel subscriptions."""

    def __init__(self):
        self._subscribers: dict[int, tuple[Any, set[str]]] = {}  # id -> (ws, channels)
        self._counter = 0

    def register(self, ws: Any, channels: set[str] | None = None) -> int:
        self._counter += 1
        self._subscribers[self._counter] = (ws, channels or {"events:*", "system"})
        return self._counter

    def unregister(self, sub_id: int) -> None:
        self._subscribers.pop(sub_id, None)

    def update_channels(self, sub_id: int, channels: set[str]) -> None:
        if sub_id in self._subscribers:
            ws, _ = self._subscribers[sub_id]
            self._subscribers[sub_id] = (ws, channels)

    async def broadcast(self, channel: str, msg_type: str, data: dict) -> None:
        """Send message to all subscribers on matching channels."""
        message = json.dumps({"channel": channel, "type": msg_type, "data": data})
        dead = []
        for sub_id, (ws, channels) in self._subscribers.items():
            if self._channel_matches(channel, channels):
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.append(sub_id)
        for sub_id in dead:
            self.unregister(sub_id)

    def _channel_matches(self, channel: str, subscribed: set[str]) -> bool:
        for sub in subscribed:
            if sub == channel:
                return True
            if sub.endswith(":*"):
                prefix = sub[:-1]  # "events:" from "events:*"
                if channel.startswith(prefix):
                    return True
        return False


class EventBus:
    """The core event bus. Publishes, dedupes, routes, and dispatches events."""

    def __init__(
        self,
        config: AppConfig,
        event_repo: EventRepository,
        assignment_repo: AssignmentRepository,
        ws_hub: WebSocketHub,
    ):
        self.config = config
        self.event_repo = event_repo
        self.assignment_repo = assignment_repo
        self.ws_hub = ws_hub
        self._router: Callable[[Event], Coroutine] | None = None
        self._listeners: list[Callable[[Event], Coroutine]] = []

    def set_router(self, router: Callable[[Event], Coroutine]) -> None:
        """Set the routing function (called after event is persisted)."""
        self._router = router

    def add_listener(self, listener: Callable[[Event], Coroutine]) -> None:
        """Add a listener that gets notified on every published event."""
        self._listeners.append(listener)

    async def publish(self, event_create: EventCreate, producer_id: str | None = None) -> Event:
        """Main entry point. Ingest → dedupe → persist → route → broadcast."""
        start = time.monotonic()

        def _observe(outcome: str) -> None:
            EVENT_PUBLISH_DURATION_SECONDS.labels(outcome=outcome).observe(
                time.monotonic() - start
            )

        # Resolve trace_id: inherit from parent, or use provided, or generate new
        trace_id = event_create.trace_id
        if not trace_id and event_create.parent_event:
            parent = await self.event_repo.get(event_create.parent_event)
            if parent and parent.trace_id:
                trace_id = parent.trace_id
        if not trace_id:
            trace_id = f"tr_{uuid4().hex[:12]}"

        # Build full event
        event = Event(
            topic=event_create.topic,
            payload=event_create.payload,
            priority=event_create.priority,
            semantic_type=event_create.semantic_type,
            dedupe_key=event_create.dedupe_key,
            parent_event=event_create.parent_event,
            output_topic=event_create.output_topic,
            context_refs=event_create.context_refs,
            memory_scope=event_create.memory_scope,
            source=event_create.source,
            trace_id=trace_id,
            producer_id=producer_id,
            expires_at=event_create.expires_at,
            max_retries=event_create.max_retries,
        )

        # Dedupe check
        if event.dedupe_key:
            existing = await self.event_repo.find_by_dedupe_key(
                event.dedupe_key, self.config.bus.dedupe_window_seconds
            )
            if existing:
                count = await self.event_repo.increment_dedupe(existing.id)
                event.status = EventStatus.deduped
                logger.info(
                    "Deduped event %s (key=%s, count=%d)",
                    event.id, event.dedupe_key, count,
                )
                record_event_deduped(event.topic)
                # Persist as deduped but don't route
                await self.event_repo.create(event)
                await self.ws_hub.broadcast(
                    f"events:{event.topic}", "event.deduped",
                    {"event_id": event.id, "original_id": existing.id, "count": count},
                )
                _observe("deduped")
                return event

        # Chain depth check
        if event.parent_event:
            depth = await self.event_repo.get_chain_depth(event.parent_event)
            if depth >= self.config.bus.max_chain_depth:
                logger.warning(
                    "Chain depth limit reached for event %s (depth=%d)", event.id, depth
                )
                record_chain_limit("depth")
                event.status = EventStatus.failed
                await self.event_repo.create(event)
                # Emit to system.chain_limit
                await self._emit_system_event("system.chain_limit", {
                    "event_id": event.id,
                    "parent_event": event.parent_event,
                    "depth": depth,
                })
                _observe("chain_depth_limited")
                return event

            # Chain budget check
            root_event = event.parent_event
            descendants = await self.event_repo.count_descendants(root_event)
            if descendants >= self.config.bus.max_chain_budget:
                logger.warning(
                    "Chain budget exceeded for root %s (descendants=%d)", root_event, descendants
                )
                record_chain_limit("budget")
                event.status = EventStatus.failed
                await self.event_repo.create(event)
                await self._emit_system_event("system.chain_limit", {
                    "event_id": event.id,
                    "root_event": root_event,
                    "descendants": descendants,
                })
                _observe("chain_budget_limited")
                return event

        # Persist
        await self.event_repo.create(event)
        record_event_published(event.topic, event.source or producer_id)
        if producer_id:
            record_producer_emit(producer_id)
        logger.info("Published event %s on topic %s", event.id, event.topic)

        # Broadcast to WebSocket
        await self.ws_hub.broadcast(
            f"events:{event.topic}", "event.new",
            event.model_dump(mode="json"),
        )

        # Route (async, don't block publish)
        if self._router:
            asyncio.create_task(self._route_event(event))

        # Notify listeners
        for listener in self._listeners:
            try:
                await listener(event)
            except Exception as e:
                logger.error("Listener error: %s", e)

        _observe("published")
        return event

    async def _route_event(self, event: Event) -> None:
        """Route event to matching consumers via the router."""
        try:
            await self._router(event)
        except Exception as e:
            logger.error("Routing error for event %s: %s", event.id, e)

    async def update_event_status(self, event_id: str, status: EventStatus) -> None:
        """Update event status and broadcast the change via WebSocket."""
        await self.event_repo.update_status(event_id, status)
        await self.ws_hub.broadcast(
            f"events:*", "event.status",
            {"id": event_id, "status": status.value},
        )

    async def _emit_system_event(self, topic: str, payload: dict) -> Event:
        """Emit a system event (for dead letter, chain limit, etc.)."""
        # Avoid infinite recursion — system events don't get routed
        event = Event(
            topic=topic,
            payload=payload,
            priority="high",
            source="system",
        )
        record_system_event(topic)
        await self.event_repo.create(event)
        await self.ws_hub.broadcast(
            f"events:{topic}", "event.new",
            event.model_dump(mode="json"),
        )
        return event

    async def publish_system_event(self, topic: str, payload: dict) -> Event:
        """Public method to emit system events."""
        return await self._emit_system_event(topic, payload)
