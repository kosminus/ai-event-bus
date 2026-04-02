"""Producer manager — lifecycle management for all producers."""

from __future__ import annotations

import logging
from typing import Any

from aiventbus.core.bus import EventBus
from aiventbus.producers.base import BaseProducer
from aiventbus.producers.clipboard import ClipboardProducer
from aiventbus.producers.file_watcher import FileWatcherProducer
from aiventbus.producers.dbus_listener import DBusListenerProducer
from aiventbus.producers.terminal_monitor import TerminalMonitorProducer
from aiventbus.producers.journald import JournaldProducer

logger = logging.getLogger(__name__)

# All known producer types and their descriptions
PRODUCER_REGISTRY: dict[str, dict[str, str]] = {
    "clipboard": {
        "description": "Monitors system clipboard for text changes (xclip/wl-paste)",
        "type": "clipboard",
    },
    "file_watcher": {
        "description": "Watches directories for file create/modify/delete events",
        "type": "file_watcher",
    },
    "dbus": {
        "description": "Listens for desktop notifications and session lock/unlock via DBus",
        "type": "dbus",
    },
    "terminal": {
        "description": "Watches shell history file for new commands",
        "type": "terminal",
    },
    "journald": {
        "description": "Streams systemd journal entries (errors, auth, service state)",
        "type": "journald",
    },
}


class ProducerManager:
    """Manages the lifecycle of event producers."""

    def __init__(self, bus: EventBus, config):
        self.bus = bus
        self.config = config
        self._producers: dict[str, BaseProducer] = {}

    def _create_producer(self, name: str) -> BaseProducer | None:
        """Create a producer instance by name."""
        cfg = self.config.producers
        if name == "clipboard":
            return ClipboardProducer(
                bus=self.bus,
                poll_interval_ms=cfg.clipboard_poll_interval_ms,
                min_length=cfg.clipboard_min_length,
            )
        elif name == "file_watcher":
            if not cfg.file_watcher_paths:
                return None
            return FileWatcherProducer(
                bus=self.bus,
                watch_paths=cfg.file_watcher_paths,
            )
        elif name == "dbus":
            return DBusListenerProducer(bus=self.bus)
        elif name == "terminal":
            return TerminalMonitorProducer(
                bus=self.bus,
                history_path=cfg.terminal_history_path,
            )
        elif name == "journald":
            return JournaldProducer(
                bus=self.bus,
                filter_noise=cfg.journald_filter_noise,
                priority_filter=cfg.journald_priority_filter,
                units=cfg.journald_units,
            )
        return None

    async def start_all(self) -> None:
        """Start all configured producers."""
        producers_cfg = self.config.producers

        if producers_cfg.clipboard_enabled:
            await self.enable("clipboard")

        if producers_cfg.file_watcher_enabled and producers_cfg.file_watcher_paths:
            await self.enable("file_watcher")

        if producers_cfg.dbus_enabled:
            await self.enable("dbus")

        if producers_cfg.terminal_monitor_enabled:
            await self.enable("terminal")

        if producers_cfg.journald_enabled:
            await self.enable("journald")

        logger.info("Started %d producers", len(self._producers))

    async def stop_all(self) -> None:
        """Stop all running producers."""
        for name in list(self._producers):
            await self.disable(name)

    async def enable(self, name: str) -> bool:
        """Start a producer by name. Returns True on success."""
        if name in self._producers and self._producers[name].is_running:
            return True
        producer = self._create_producer(name)
        if not producer:
            logger.warning("Cannot create producer: %s", name)
            return False
        self._producers[name] = producer
        await producer.start()
        logger.info("Producer enabled: %s", name)
        return True

    async def disable(self, name: str) -> bool:
        """Stop a producer by name. Returns True if it was running."""
        producer = self._producers.pop(name, None)
        if producer and producer.is_running:
            await producer.stop()
            logger.info("Producer disabled: %s", name)
            return True
        return False

    def get(self, name: str) -> BaseProducer | None:
        return self._producers.get(name)

    def list_running(self) -> list[str]:
        return [name for name, p in self._producers.items() if p.is_running]

    def list_all(self) -> list[dict[str, Any]]:
        """Return status of all known producers."""
        result = []
        for name, info in PRODUCER_REGISTRY.items():
            producer = self._producers.get(name)
            result.append({
                "name": name,
                "type": info["type"],
                "description": info["description"],
                "running": producer.is_running if producer else False,
            })
        return result
