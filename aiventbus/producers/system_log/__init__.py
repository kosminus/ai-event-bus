"""Unified system-log producer.

One producer, two backends: ``backends.journald`` on Linux and
``backends.log_stream`` on macOS. Both produce events on the same
``syslog.*`` topics with identical payload shapes so downstream agents
and routing rules don't need to know which backend is active.

The backend is chosen by ``build_default_backend`` based on
``aiventbus.platform.os_id()``; the producer itself is OS-agnostic and
only concerns itself with: spawning the backend's subprocess, reading
NDJSON lines, applying the shared priority gate (with auth/service
bypass), classifying each entry into a topic, and publishing.

Adding a new OS means writing a third backend that implements
``SystemLogBackend`` and wiring it into ``build_default_backend``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from aiventbus import platform as _platform
from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared classification — operates on the normalized journal-compat dict
# that every backend returns from ``parse_line``.
# ---------------------------------------------------------------------------

# Journal numeric priorities (RFC 5424 / syslog).
_PRIO_ERROR = 3  # emerg(0), alert(1), crit(2), err(3)
_PRIO_WARNING = 4

# Facilities / identifiers we always route to syslog.auth.
_AUTH_FACILITIES = {"4", "10"}  # auth(4), authpriv(10)
_AUTH_IDENTIFIERS = {"sshd", "sudo", "su", "login", "systemd-logind", "pam", "polkitd"}

# macOS-specific auth hints that surface via the ``_SUBSYSTEM`` extra key.
_MACOS_AUTH_SUBSYSTEMS = {
    "com.apple.authd",
    "com.apple.opendirectoryd",
    "com.apple.securityd",
    "com.apple.securityd.xpc",
}

# Service-lifecycle keywords used by both journald (systemd start/stop) and
# launchd (on macOS via ``launchd``/``com.apple.xpc.launchd``).
_SERVICE_KEYWORDS = ("started", "stopped", "failed", "starting", "stopping", "exited")


def classify_topic(entry: dict[str, Any]) -> str:
    """Map a normalized log entry to a ``syslog.*`` topic."""
    prio = int(entry.get("PRIORITY", 6))
    facility = entry.get("SYSLOG_FACILITY", "")
    ident = entry.get("SYSLOG_IDENTIFIER", "") or entry.get("_COMM", "")
    unit = entry.get("_SYSTEMD_UNIT", "")
    message = entry.get("MESSAGE", "")
    subsystem = entry.get("_SUBSYSTEM", "")

    # Auth: syslog facility OR known auth unit OR macOS auth subsystem.
    if (
        facility in _AUTH_FACILITIES
        or ident.lower() in _AUTH_IDENTIFIERS
        or subsystem in _MACOS_AUTH_SUBSYSTEMS
    ):
        return "syslog.auth"

    # Service lifecycle: systemd on Linux, launchd on macOS.
    is_systemd = ident == "systemd" and unit == "init.scope"
    is_launchd = ident == "launchd" or subsystem == "com.apple.xpc.launchd"
    if is_systemd or is_launchd:
        lower = message.lower()
        if any(kw in lower for kw in _SERVICE_KEYWORDS):
            return "syslog.service"

    if prio <= _PRIO_ERROR:
        return "syslog.error"
    if prio == _PRIO_WARNING:
        return "syslog.warning"
    return "syslog.info"


def _topic_priority(topic: str) -> Priority:
    if topic == "syslog.auth":
        return Priority.high
    if topic in ("syslog.error", "syslog.service"):
        return Priority.medium
    return Priority.low


# ---------------------------------------------------------------------------
# Backend contract
# ---------------------------------------------------------------------------

class SystemLogBackend(Protocol):
    """One OS implementation of system-log ingestion."""

    name: str                # "journald" | "log_stream"
    producer_source: str     # "producer:journald" | "producer:log_stream"
    producer_id: str         # "producer_journald" | "producer_log_stream"

    def build_cmd(self) -> list[str]:
        """Return the command to spawn to start streaming log entries."""

    def parse_line(self, raw: bytes) -> dict[str, Any] | None:
        """Parse one raw NDJSON line from the backend's subprocess.

        Returns a normalized dict compatible with ``classify_topic`` and
        the publish payload below, or ``None`` to drop the line (e.g.
        parse failure, empty message, backend-specific noise).
        """


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------

class SystemLogProducer(BaseProducer):
    """Streams system-log entries via a pluggable backend."""

    # Topics that bypass the priority filter — auth and service events are
    # important even when the backend emits them at info level.
    _BYPASS_TOPICS = {"syslog.auth", "syslog.service"}

    def __init__(
        self,
        bus: EventBus,
        backend: SystemLogBackend,
        priority_filter: int = 4,
    ):
        self.bus = bus
        self.backend = backend
        self.priority_filter = priority_filter
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._stream_loop())
        logger.info("system_log producer started (backend=%s)", self.backend.name)

    async def stop(self) -> None:
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                if self._proc:
                    self._proc.kill()
            self._proc = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("system_log producer stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _stream_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                cmd = self.backend.build_cmd()
                logger.debug("Spawning system_log backend: %s", " ".join(cmd))
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                backoff = 1.0

                assert self._proc.stdout is not None
                async for line in self._proc.stdout:
                    if not self._running:
                        break
                    await self._handle_line(line)

                if self._running:
                    logger.warning(
                        "system_log backend exited unexpectedly, restarting in %.0fs",
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("system_log stream error")
                if self._running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    async def _handle_line(self, raw: bytes) -> None:
        entry = self.backend.parse_line(raw)
        if entry is None:
            return
        message = entry.get("MESSAGE", "")
        if not message:
            return

        topic = classify_topic(entry)
        prio = int(entry.get("PRIORITY", 6))
        if prio > self.priority_filter and topic not in self._BYPASS_TOPICS:
            return

        await self.bus.publish(
            EventCreate(
                topic=topic,
                payload={
                    "message": str(message)[:2000],
                    "identifier": str(entry.get("SYSLOG_IDENTIFIER", "")
                                      or entry.get("_COMM", "")),
                    "unit": str(entry.get("_SYSTEMD_UNIT", "")),
                    "pid": str(entry.get("_PID", "")),
                    "priority": prio,
                    "facility": str(entry.get("SYSLOG_FACILITY", "")),
                    "subsystem": str(entry.get("_SUBSYSTEM", "")),
                    "category": str(entry.get("_CATEGORY", "")),
                },
                priority=_topic_priority(topic),
                dedupe_key=None,
                source=self.backend.producer_source,
            ),
            producer_id=self.backend.producer_id,
        )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def build_default_backend(
    *,
    filter_noise: bool = True,
    units: list[str] | None = None,
    predicate_override: str | None = None,
) -> SystemLogBackend | None:
    """Return the backend appropriate for this OS, or ``None`` if no
    system-log backend is wired up for the current platform.
    """
    os_id = _platform.os_id()
    if os_id == "linux":
        from aiventbus.producers.system_log.backends.journald import JournaldBackend

        return JournaldBackend(filter_noise=filter_noise, units=units or [])
    if os_id == "darwin":
        from aiventbus.producers.system_log.backends.log_stream import LogStreamBackend

        return LogStreamBackend(
            filter_noise=filter_noise,
            predicate_override=predicate_override,
        )
    return None


__all__ = [
    "SystemLogProducer",
    "SystemLogBackend",
    "build_default_backend",
    "classify_topic",
]
