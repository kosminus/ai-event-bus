"""DBus listener producer — subscribes to session bus signals.

Listens for desktop notifications, session lock/unlock, and media player state.
Uses dbus-fast (async, native asyncio).
"""

from __future__ import annotations

import asyncio
import logging

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


class DBusListenerProducer(BaseProducer):
    """Subscribes to DBus session bus signals and publishes events."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._running = False
        self._dbus_conn = None
        self._signal_conn = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._listen_loop())
        logger.info("DBus listener started")

    async def stop(self) -> None:
        self._running = False
        for conn in (self._dbus_conn, self._signal_conn):
            if conn:
                conn.disconnect()
        self._dbus_conn = None
        self._signal_conn = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("DBus listener stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _listen_loop(self) -> None:
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast import MessageType, Message

            # We need TWO connections:
            # 1. A monitor connection (becomes read-only) to eavesdrop on Notify method calls
            # 2. A normal connection for signal subscriptions (lock/unlock)

            # --- Monitor connection for notifications ---
            self._dbus_conn = await MessageBus().connect()

            monitor_rules = [
                "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
            ]
            try:
                await self._dbus_conn.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus.Monitoring",
                        member="BecomeMonitor",
                        signature="asu",
                        body=[monitor_rules, 0],
                    )
                )
                logger.info("DBus: using Monitor interface for notification capture")
            except Exception as e:
                # Fallback for older dbus-daemon without Monitor support
                logger.warning("DBus: BecomeMonitor failed (%s), notifications may not be captured", e)

            def monitor_handler(msg: Message) -> None:
                if msg.member == "Notify" and msg.interface == "org.freedesktop.Notifications":
                    asyncio.create_task(self._handle_notification(msg))

            self._dbus_conn.add_message_handler(monitor_handler)

            # --- Signal connection for session lock/unlock ---
            self._signal_conn = await MessageBus().connect()

            for member in ("Lock", "Unlock"):
                await self._signal_conn.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus",
                        member="AddMatch",
                        signature="s",
                        body=[
                            f"type='signal',interface='org.freedesktop.login1.Session',member='{member}'"
                        ],
                    )
                )

            def signal_handler(msg: Message) -> None:
                if msg.message_type == MessageType.SIGNAL:
                    if msg.member == "Lock":
                        asyncio.create_task(self._handle_session_event("session.locked"))
                    elif msg.member == "Unlock":
                        asyncio.create_task(self._handle_session_event("session.unlocked"))

            self._signal_conn.add_message_handler(signal_handler)

            # Keep alive until stopped
            while self._running:
                await asyncio.sleep(1)

        except ImportError:
            logger.warning("dbus-fast not installed — DBus listener disabled (pip install dbus-fast)")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("DBus listener error: %s", e)

    async def _handle_notification(self, msg) -> None:
        try:
            body = msg.body
            # Notify signature: susssasa{sv}i
            # [app_name, replaces_id, icon, summary, body, actions, hints, timeout]
            app_name = body[0] if len(body) > 0 else "unknown"
            summary = body[3] if len(body) > 3 else ""
            notif_body = body[4] if len(body) > 4 else ""

            await self.bus.publish(
                EventCreate(
                    topic="notification.received",
                    payload={
                        "app_name": str(app_name),
                        "summary": str(summary),
                        "body": str(notif_body)[:500],
                    },
                    priority=Priority.low,
                    source="producer:dbus",
                ),
                producer_id="producer_dbus",
            )
        except Exception as e:
            logger.debug("Failed to handle notification: %s", e)

    async def _handle_session_event(self, topic: str) -> None:
        await self.bus.publish(
            EventCreate(
                topic=topic,
                payload={"event": topic},
                priority=Priority.medium,
                source="producer:dbus",
            ),
            producer_id="producer_dbus",
        )
        logger.info("Session event: %s", topic)
