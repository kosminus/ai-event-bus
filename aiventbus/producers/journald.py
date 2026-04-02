"""Journald producer — streams systemd journal entries as events.

Uses ``journalctl -f -o json`` to follow the system journal in real time.
Classifies entries by priority and facility into topics:

- ``syslog.error``   — priority 0-3 (emerg, alert, crit, err)
- ``syslog.warning`` — priority 4 (warning)
- ``syslog.auth``    — auth/authpriv facility OR sshd/sudo/su units
- ``syslog.service`` — systemd service state changes (started, stopped, failed)
- ``syslog.info``    — everything else (priority 5-7)

Filtering strategy (default: priority_filter=4, i.e. warning+):
- Priority filtering is done in Python, NOT via ``journalctl -p``, so that
  auth and service events always pass through regardless of their syslog
  priority (successful logins and service starts are info-level but important).
- A noise filter drops known high-frequency / low-value sources (timesyncd,
  resolved, NetworkManager, session slices, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)

# Journal numeric priorities (RFC 5424 / syslog)
_PRIO_ERROR = 3  # emerg(0), alert(1), crit(2), err(3)
_PRIO_WARNING = 4
_PRIO_NOTICE = 5

# Facilities that indicate auth-related messages
_AUTH_FACILITIES = {"4", "10"}  # auth(4), authpriv(10)

# Units / identifiers strongly associated with auth
_AUTH_IDENTIFIERS = {"sshd", "sudo", "su", "login", "systemd-logind", "pam", "polkitd"}

# Noisy identifiers to ignore by default (reduce event flood)
_NOISE_IDENTIFIERS = {
    "systemd-timesyncd",
    "systemd-resolved",
    "systemd-networkd",
    "NetworkManager",
    "dhclient",
    "avahi-daemon",
    "rtkit-daemon",
    "dbus-daemon",
    "pulseaudio",
    "pipewire",
    "snapd",
    "packagekitd",
}

# Noisy unit patterns (slice/scope churn)
_NOISE_UNIT_PREFIXES = (
    "session-",
    "user@",
    "user-runtime-dir@",
    "run-",
    "snap.",
)


def _is_noisy(entry: dict) -> bool:
    """Return True if the journal entry is likely noise."""
    ident = entry.get("SYSLOG_IDENTIFIER", "") or entry.get("_COMM", "")
    if ident in _NOISE_IDENTIFIERS:
        return True
    unit = entry.get("_SYSTEMD_UNIT", "")
    if any(unit.startswith(p) for p in _NOISE_UNIT_PREFIXES):
        return True
    return False


def _classify_topic(entry: dict) -> str:
    """Map a journal entry to a topic string."""
    prio = int(entry.get("PRIORITY", 6))
    facility = entry.get("SYSLOG_FACILITY", "")
    ident = entry.get("SYSLOG_IDENTIFIER", "") or entry.get("_COMM", "")
    unit = entry.get("_SYSTEMD_UNIT", "")
    message = entry.get("MESSAGE", "")

    # Auth events (facility or known auth unit)
    if facility in _AUTH_FACILITIES or ident.lower() in _AUTH_IDENTIFIERS:
        return "syslog.auth"

    # Service lifecycle (systemd reporting unit state changes)
    if ident == "systemd" and unit == "init.scope":
        lower = message.lower()
        if any(kw in lower for kw in ("started", "stopped", "failed", "starting", "stopping")):
            return "syslog.service"

    # Priority-based classification
    if prio <= _PRIO_ERROR:
        return "syslog.error"
    if prio == _PRIO_WARNING:
        return "syslog.warning"

    return "syslog.info"


def _classify_priority(topic: str) -> Priority:
    """Map topic to event bus priority."""
    if topic == "syslog.auth":
        return Priority.high
    if topic in ("syslog.error", "syslog.service"):
        return Priority.medium
    if topic == "syslog.warning":
        return Priority.low
    return Priority.low


class JournaldProducer(BaseProducer):
    """Streams systemd journal entries as events via ``journalctl -f``."""

    def __init__(
        self,
        bus: EventBus,
        filter_noise: bool = True,
        priority_filter: int = 4,
        units: list[str] | None = None,
    ):
        self.bus = bus
        self.filter_noise = filter_noise
        self.priority_filter = priority_filter  # 4=warning+, 7=all
        self.units = units or []
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._running = False

    async def start(self) -> None:
        if not shutil.which("journalctl"):
            logger.warning("journalctl not found — journald producer disabled")
            return
        self._running = True
        self._task = asyncio.create_task(self._stream_loop())
        logger.info("Journald producer started")

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
        logger.info("Journald producer stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def _build_cmd(self) -> list[str]:
        """Build the journalctl command.

        Priority filtering is NOT done here — it's done in _handle_line so
        that auth/service events always pass through even at info level.
        Unit filtering IS done here (reduces journal read I/O).
        """
        cmd = ["journalctl", "-f", "-o", "json", "--no-pager"]
        for unit in self.units:
            cmd += ["-u", unit]
        return cmd

    async def _stream_loop(self) -> None:
        """Main loop: spawn journalctl and process lines."""
        backoff = 1.0
        while self._running:
            try:
                cmd = self._build_cmd()
                logger.debug("Spawning: %s", " ".join(cmd))
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                backoff = 1.0  # reset on successful start

                assert self._proc.stdout is not None
                async for line in self._proc.stdout:
                    if not self._running:
                        break
                    await self._handle_line(line)

                # Process exited
                if self._running:
                    logger.warning("journalctl exited unexpectedly, restarting in %.0fs", backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Journald stream error: %s", e)
                if self._running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    # Topics that bypass the priority filter (always important)
    _BYPASS_TOPICS = {"syslog.auth", "syslog.service"}

    async def _handle_line(self, raw: bytes) -> None:
        """Parse one JSON line from journalctl output."""
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return

        # Apply noise filter (known spammy sources)
        if self.filter_noise and _is_noisy(entry):
            return

        message = entry.get("MESSAGE", "")
        if not message:
            return

        topic = _classify_topic(entry)
        prio = int(entry.get("PRIORITY", 6))

        # Priority gate: drop entries below threshold UNLESS they are
        # auth or service events (those are security/ops-relevant even
        # when logged at info level, e.g. successful ssh login = prio 6)
        if prio > self.priority_filter and topic not in self._BYPASS_TOPICS:
            return

        ident = entry.get("SYSLOG_IDENTIFIER", "") or entry.get("_COMM", "")
        unit = entry.get("_SYSTEMD_UNIT", "")
        pid = entry.get("_PID", "")

        await self.bus.publish(
            EventCreate(
                topic=topic,
                payload={
                    "message": str(message)[:2000],
                    "identifier": str(ident),
                    "unit": str(unit),
                    "pid": str(pid),
                    "priority": prio,
                    "facility": entry.get("SYSLOG_FACILITY", ""),
                },
                priority=_classify_priority(topic),
                dedupe_key=None,
                source="producer:journald",
            ),
            producer_id="producer_journald",
        )
