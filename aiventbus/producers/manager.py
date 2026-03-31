"""Producer manager — lifecycle management for all producers."""

from __future__ import annotations

import logging

from aiventbus.core.bus import EventBus
from aiventbus.producers.base import BaseProducer
from aiventbus.producers.clipboard import ClipboardProducer
from aiventbus.producers.file_watcher import FileWatcherProducer
from aiventbus.producers.dbus_listener import DBusListenerProducer
from aiventbus.producers.terminal_monitor import TerminalMonitorProducer

logger = logging.getLogger(__name__)


class ProducerManager:
    """Manages the lifecycle of event producers."""

    def __init__(self, bus: EventBus, config):
        self.bus = bus
        self.config = config
        self._producers: dict[str, BaseProducer] = {}

    async def start_all(self) -> None:
        """Start all configured producers."""
        producers_cfg = self.config.producers

        if producers_cfg.clipboard_enabled:
            clipboard = ClipboardProducer(
                bus=self.bus,
                poll_interval_ms=producers_cfg.clipboard_poll_interval_ms,
                min_length=producers_cfg.clipboard_min_length,
            )
            self._producers["clipboard"] = clipboard
            await clipboard.start()

        if producers_cfg.file_watcher_enabled and producers_cfg.file_watcher_paths:
            file_watcher = FileWatcherProducer(
                bus=self.bus,
                watch_paths=producers_cfg.file_watcher_paths,
            )
            self._producers["file_watcher"] = file_watcher
            await file_watcher.start()

        if producers_cfg.dbus_enabled:
            dbus = DBusListenerProducer(bus=self.bus)
            self._producers["dbus"] = dbus
            await dbus.start()

        if producers_cfg.terminal_monitor_enabled:
            terminal = TerminalMonitorProducer(
                bus=self.bus,
                history_path=producers_cfg.terminal_history_path,
            )
            self._producers["terminal"] = terminal
            await terminal.start()

        logger.info("Started %d producers", len(self._producers))

    async def stop_all(self) -> None:
        """Stop all running producers."""
        for name, producer in self._producers.items():
            if producer.is_running:
                await producer.stop()
        self._producers.clear()

    def get(self, name: str) -> BaseProducer | None:
        return self._producers.get(name)

    def list_running(self) -> list[str]:
        return [name for name, p in self._producers.items() if p.is_running]
