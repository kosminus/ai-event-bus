"""Unified desktop-events producer.

One producer, two backends:

- ``backends.dbus`` — Linux: DBus session-bus monitor (session lock /
  unlock via ``org.freedesktop.login1.Session``, notification capture via
  ``org.freedesktop.Notifications`` Monitor interface).
- ``backends.mac_helper`` — macOS: spawns the Swift sidecar binary
  ``aiventbus-mac-helper`` and parses its versioned NDJSON stream for
  screen lock/unlock and ``NSWorkspace`` app-lifecycle events.

Backends differ too much to share a single subprocess shell like
``system_log`` does (DBus is in-process; the Swift helper is a child
subprocess), so each backend is itself a ``BaseProducer``. The
``DesktopEventsProducer`` wrapper picks the right one via
``build_default_backend`` and forwards lifecycle calls. Downstream, the
bus sees identical topics and payload shapes regardless of which
backend is active.
"""

from __future__ import annotations

import logging

from aiventbus import platform as _platform
from aiventbus.core.bus import EventBus
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


class DesktopEventsProducer(BaseProducer):
    """Wrapper that delegates to the backend appropriate for this OS."""

    def __init__(self, backend: BaseProducer):
        self.backend = backend

    async def start(self) -> None:
        await self.backend.start()

    async def stop(self) -> None:
        await self.backend.stop()

    @property
    def is_running(self) -> bool:
        return self.backend.is_running


def build_default_backend(bus: EventBus) -> BaseProducer | None:
    """Return the backend for this OS, or ``None`` if one isn't wired up."""
    os_id = _platform.os_id()
    if os_id == "linux":
        from aiventbus.producers.desktop_events.backends.dbus import DBusBackend

        return DBusBackend(bus=bus)
    if os_id == "darwin":
        from aiventbus.producers.desktop_events.backends.mac_helper import (
            MacHelperBackend,
        )

        helper = _platform.mac_helper_path()
        if helper is None:
            logger.info(
                "macOS helper not installed — desktop_events unavailable "
                "until `aibus install --build-helper`"
            )
            return None
        return MacHelperBackend(bus=bus, helper_path=helper)
    return None


__all__ = ["DesktopEventsProducer", "build_default_backend"]
