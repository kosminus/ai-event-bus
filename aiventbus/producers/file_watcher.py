"""File watcher producer — monitors directories for file changes via watchfiles."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path

from watchfiles import awatch, Change

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)

_CHANGE_TOPICS = {
    Change.added: "fs.created",
    Change.modified: "fs.modified",
    Change.deleted: "fs.deleted",
}


class FileWatcherProducer(BaseProducer):
    """Watches configured directories and publishes fs.* events on changes."""

    def __init__(
        self,
        bus: EventBus,
        watch_paths: list[str],
        ignore_patterns: list[str] | None = None,
    ):
        self.bus = bus
        self.watch_paths = [str(Path(p).expanduser().resolve()) for p in watch_paths]
        self.ignore_patterns = ignore_patterns or [
            "*.swp", "*.tmp", "*~", ".git/*", "__pycache__/*", "*.pyc",
        ]
        self._task: asyncio.Task | None = None
        self._running = False
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        existing = [p for p in self.watch_paths if Path(p).exists()]
        if not existing:
            logger.warning("File watcher: no valid paths to watch from %s", self.watch_paths)
            return
        self.watch_paths = existing
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("File watcher started on: %s", self.watch_paths)

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("File watcher stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def _should_ignore(self, path: str) -> bool:
        p = Path(path)
        for pattern in self.ignore_patterns:
            if p.match(pattern):
                return True
        return False

    async def _watch_loop(self) -> None:
        try:
            async for changes in awatch(
                *self.watch_paths,
                stop_event=self._stop_event,
                recursive=True,
            ):
                if not self._running:
                    break
                for change_type, path_str in changes:
                    if self._should_ignore(path_str):
                        continue
                    await self._publish_change(change_type, path_str)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("File watcher error: %s", e)

    async def _publish_change(self, change_type: Change, path_str: str) -> None:
        topic = _CHANGE_TOPICS.get(change_type, "fs.changed")
        p = Path(path_str)
        mimetype, _ = mimetypes.guess_type(path_str)

        payload = {
            "path": path_str,
            "filename": p.name,
            "directory": str(p.parent),
            "mimetype": mimetype,
        }

        # Add size for non-deleted files
        if change_type != Change.deleted:
            try:
                payload["size_bytes"] = p.stat().st_size
            except OSError:
                pass

        await self.bus.publish(
            EventCreate(
                topic=topic,
                payload=payload,
                priority=Priority.low,
                dedupe_key=f"fs:{change_type.name}:{path_str}",
                source="producer:file_watcher",
            ),
            producer_id="producer_file_watcher",
        )
        logger.debug("File event: %s %s", topic, p.name)
