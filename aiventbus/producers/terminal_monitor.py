"""Terminal monitor producer — watches shell history for new commands."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


def _detect_history_file() -> Path | None:
    """Find the shell history file."""
    shell = os.environ.get("SHELL", "/bin/bash")

    candidates = []
    if "zsh" in shell:
        candidates = [
            Path.home() / ".zsh_history",
            Path.home() / ".local/share/zsh/history",
        ]
    elif "bash" in shell:
        candidates = [
            Path.home() / ".bash_history",
        ]
    # Also check HISTFILE env var
    histfile = os.environ.get("HISTFILE")
    if histfile:
        candidates.insert(0, Path(histfile))

    for path in candidates:
        if path.exists():
            return path
    return None


class TerminalMonitorProducer(BaseProducer):
    """Watches shell history file and publishes terminal.command events."""

    def __init__(
        self,
        bus: EventBus,
        history_path: str | None = None,
        poll_interval_s: float = 2.0,
    ):
        self.bus = bus
        self._history_path = Path(history_path) if history_path else _detect_history_file()
        self.poll_interval_s = poll_interval_s
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_size: int = 0
        self._last_line_count: int = 0

    async def start(self) -> None:
        if not self._history_path or not self._history_path.exists():
            logger.warning("Terminal monitor: no history file found, disabled")
            return
        self._running = True
        # Initialize to current end of file so we only emit new commands
        self._last_size = self._history_path.stat().st_size
        with open(self._history_path, "rb") as f:
            self._last_line_count = sum(1 for _ in f)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Terminal monitor started (watching %s)", self._history_path)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Terminal monitor stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._check_new_commands()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Terminal monitor error: %s", e)
            await asyncio.sleep(self.poll_interval_s)

    async def _check_new_commands(self) -> None:
        try:
            current_size = self._history_path.stat().st_size
        except OSError:
            return

        if current_size <= self._last_size:
            return

        # Read new lines
        new_commands = []
        try:
            with open(self._history_path, "r", errors="replace") as f:
                all_lines = f.readlines()
                new_lines = all_lines[self._last_line_count:]
                self._last_line_count = len(all_lines)
                self._last_size = current_size

                for line in new_lines:
                    cmd = self._parse_history_line(line.strip())
                    if cmd:
                        new_commands.append(cmd)
        except OSError:
            return

        for cmd in new_commands[-10:]:  # Cap at 10 per check
            await self.bus.publish(
                EventCreate(
                    topic="terminal.command",
                    payload={
                        "command": cmd,
                        "shell": os.environ.get("SHELL", "unknown"),
                    },
                    priority=Priority.low,
                    source="producer:terminal",
                ),
                producer_id="producer_terminal",
            )

    def _parse_history_line(self, line: str) -> str | None:
        """Parse a history line, handling zsh extended format."""
        if not line:
            return None
        # zsh extended format: ": 1234567890:0;command"
        if line.startswith(": ") and ";" in line:
            return line.split(";", 1)[1].strip() or None
        return line
