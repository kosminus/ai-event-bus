"""Clipboard monitor producer — watches for clipboard changes.

On Wayland: uses `wl-paste --watch` (event-driven, no polling flicker).
On X11: polls `xclip` at configured interval.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


def _is_wayland() -> bool:
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    return session_type == "wayland" or bool(wayland_display)


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
        self._wayland = _is_wayland()
        self._last_hash: str | None = None
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        if self._wayland:
            # Try event-driven first, fall back to polling
            if await self._watch_supported():
                self._task = asyncio.create_task(self._watch_loop())
                logger.info("Clipboard producer started (wl-paste --watch, event-driven)")
            else:
                self._task = asyncio.create_task(self._poll_loop_wayland())
                logger.info("Clipboard producer started (wl-paste polling, interval=%dms)", int(self.poll_interval_s * 1000))
        else:
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("Clipboard producer started (xclip polling, interval=%dms)", int(self.poll_interval_s * 1000))

    async def _watch_supported(self) -> bool:
        """Check if wl-paste --watch is supported by the compositor."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "wl-paste", "--watch", "true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            return proc.returncode == 0
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            return False

    async def stop(self) -> None:
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            self._proc = None
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

    # --- Wayland: event-driven via wl-paste --watch ---

    async def _watch_loop(self) -> None:
        """Use `wl-paste --watch` to run a command on each clipboard change.

        `wl-paste --watch <cmd>` executes <cmd> every time the clipboard changes.
        We use `echo CLIP_CHANGED` as the command — each time we see a line,
        we know the clipboard changed and we read it with a one-shot `wl-paste`.
        """
        while self._running:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    "wl-paste", "--watch", "echo", "CLIP_CHANGED",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                while self._running and self._proc.returncode is None:
                    try:
                        line = await asyncio.wait_for(
                            self._proc.stdout.readline(),
                            timeout=60.0,
                        )
                    except asyncio.TimeoutError:
                        continue
                    if not line:
                        break  # Process ended

                    # Clipboard changed — read current content with one-shot wl-paste
                    content = await self._read_clipboard_wayland()
                    if content:
                        await self._publish_content(content)

            except asyncio.CancelledError:
                break
            except FileNotFoundError:
                logger.warning("wl-paste not found — install wl-clipboard")
                break
            except Exception as e:
                logger.debug("Clipboard watch error: %s", e)

            # If the process died unexpectedly, restart after a brief pause
            if self._running:
                await asyncio.sleep(1)

    async def _read_clipboard_wayland(self) -> str | None:
        """One-shot read of current clipboard content."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "wl-paste", "--no-newline",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0 and stdout:
                return stdout.decode("utf-8", errors="replace").strip()
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            pass
        return None

    # --- Wayland: polling fallback (when --watch not supported) ---

    async def _poll_loop_wayland(self) -> None:
        while self._running:
            try:
                content = await self._read_clipboard_wayland()
                if content:
                    await self._publish_content(content)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Clipboard read error: %s", e)
            await asyncio.sleep(self.poll_interval_s)

    # --- X11: polling fallback ---

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                content = await self._read_clipboard_x11()
                if content:
                    await self._publish_content(content)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Clipboard read error: %s", e)

            await asyncio.sleep(self.poll_interval_s)

    async def _read_clipboard_x11(self) -> str | None:
        """Read text from X11 clipboard via xclip."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xclip", "-selection", "clipboard", "-o",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0 and stdout:
                return stdout.decode("utf-8", errors="replace").strip()
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            pass
        return None
