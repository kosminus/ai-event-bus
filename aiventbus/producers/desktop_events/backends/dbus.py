"""DBus backend — Linux desktop-events source.

Absorbs the former standalone ``aiventbus/producers/dbus_listener.py``.
Monitors ``org.freedesktop.Notifications`` for inbound notifications
and ``org.freedesktop.login1.Session`` for screen lock / unlock.
"""

from __future__ import annotations

import asyncio
import logging

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


class DBusBackend(BaseProducer):
    """Subscribes to DBus session-bus signals and publishes events."""

    name = "dbus"

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._running = False
        self._dbus_conn = None
        self._signal_conn = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._listen_loop())
        logger.info("desktop_events: dbus backend started")

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
        logger.info("desktop_events: dbus backend stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _listen_loop(self) -> None:
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast import MessageType, Message

            # Monitor connection (becomes read-only) to eavesdrop on
            # Notify method calls.
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
                logger.info("desktop_events: DBus Monitor connected for notifications")
            except Exception as e:
                logger.warning(
                    "desktop_events: BecomeMonitor failed (%s); "
                    "notifications may not be captured",
                    e,
                )

            def monitor_handler(msg: Message) -> None:
                if (
                    msg.member == "Notify"
                    and msg.interface == "org.freedesktop.Notifications"
                ):
                    asyncio.create_task(self._handle_notification(msg))

            self._dbus_conn.add_message_handler(monitor_handler)

            # Signal connection for session lock/unlock.
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
                        asyncio.create_task(self._handle_session("session.locked"))
                    elif msg.member == "Unlock":
                        asyncio.create_task(self._handle_session("session.unlocked"))

            self._signal_conn.add_message_handler(signal_handler)

            while self._running:
                await asyncio.sleep(1)

        except ImportError:
            logger.warning(
                "desktop_events: dbus-fast not installed — backend disabled "
                "(pip install dbus-fast)"
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("desktop_events: dbus backend error: %s", e)

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
            logger.debug("desktop_events: failed to handle notification: %s", e)

    async def _handle_session(self, topic: str) -> None:
        await self.bus.publish(
            EventCreate(
                topic=topic,
                payload={"event": topic},
                priority=Priority.medium,
                source="producer:dbus",
            ),
            producer_id="producer_dbus",
        )
        logger.info("desktop_events: session event %s", topic)
