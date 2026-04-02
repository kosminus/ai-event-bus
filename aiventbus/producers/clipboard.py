"""Clipboard monitor producer — watches for clipboard changes.

Uses xclip polling via XWayland (works on both Wayland and X11, no flicker).
Falls back to wl-paste if xclip is unavailable on Wayland.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


class ClipboardProducer(BaseProducer):
    """Watches the system clipboard and publishes text content as events."""

    def __init__(
        self,
        bus: EventBus,
        poll_interval_ms: int = 500,
        min_length: int = 10,
    ):
        self.bus = bus
        self.poll_interval_s = poll_interval_ms / 1000.0
        self.min_length = min_length
        self._last_hash: str | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        if shutil.which("xclip"):
            self._task = asyncio.create_task(self._poll_loop_xclip())
            logger.info(
                "Clipboard producer started (xclip, interval=%dms)",
                int(self.poll_interval_s * 1000),
            )
        elif shutil.which("wl-paste"):
            self._task = asyncio.create_task(self._poll_loop_wlpaste())
            logger.info(
                "Clipboard producer started (wl-paste polling, interval=%dms)",
                int(self.poll_interval_s * 1000),
            )
        else:
            logger.warning("No clipboard tool found — install xclip or wl-clipboard")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Clipboard producer stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _publish_content(self, content: str) -> None:
        """Publish clipboard content if it's new and long enough."""
        if len(content) < self.min_length:
            return
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if content_hash == self._last_hash:
            return
        self._last_hash = content_hash
        await self.bus.publish(
            EventCreate(
                topic="clipboard.text",
                payload={
                    "content": content,
                    "content_hash": content_hash,
                    "length": len(content),
                },
                priority=Priority.low,
                dedupe_key=f"clip:{content_hash}",
                source="producer:clipboard",
            ),
            producer_id="producer_clipboard",
        )
        logger.debug("Clipboard event published (%d chars)", len(content))

    async def _read_clipboard(self, *cmd: str) -> str | None:
        """Run a clipboard read command and return text content."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0 and stdout:
                return stdout.decode("utf-8", errors="replace").strip()
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            pass
        return None

    async def _poll_loop_xclip(self) -> None:
        """Poll clipboard via xclip (works on X11 and Wayland via XWayland)."""
        while self._running:
            try:
                content = await self._read_clipboard(
                    "xclip", "-selection", "clipboard", "-o"
                )
                if content:
                    await self._publish_content(content)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Clipboard read error: %s", e)
            await asyncio.sleep(self.poll_interval_s)

    async def _poll_loop_wlpaste(self) -> None:
        """Poll clipboard via wl-paste (Wayland fallback)."""
        while self._running:
            try:
                content = await self._read_clipboard(
                    "wl-paste", "--no-newline", "--type", "text/plain"
                )
                if content:
                    await self._publish_content(content)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Clipboard read error: %s", e)
            await asyncio.sleep(self.poll_interval_s)
