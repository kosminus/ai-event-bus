"""macOS backend — consumes NDJSON from the ``aiventbus-mac-helper`` sidecar.

Wire format (version 1) is defined by the Swift helper in
``bin/aiventbus-mac-helper/Sources/aiventbus-mac-helper/main.swift``::

    {"v":1,"type":"session.locked","ts":"2026-04-17T14:32:17Z","payload":{}}
    {"v":1,"type":"session.unlocked","ts":"...","payload":{}}
    {"v":1,"type":"app.launched","ts":"...","payload":{"bundle_id":"...","pid":1234,"name":"..."}}
    {"v":1,"type":"app.terminated","ts":"...","payload":{"bundle_id":"...","pid":1234}}
    {"v":1,"type":"app.activated","ts":"...","payload":{"bundle_id":"...","name":"..."}}
    {"v":1,"type":"helper.ready","ts":"...","payload":{"version":1}}  # internal handshake

Unknown types are logged and dropped. If the helper crashes, we
restart it with exponential backoff (cap 30 s).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


# Map of helper event types to (bus topic, Priority). ``helper.ready`` is
# intentionally absent — it's a handshake, not a bus event.
_TOPIC_MAP: dict[str, tuple[str, Priority]] = {
    "session.locked":   ("session.locked",   Priority.medium),
    "session.unlocked": ("session.unlocked", Priority.medium),
    "app.launched":     ("app.launched",     Priority.low),
    "app.terminated":   ("app.terminated",   Priority.low),
    "app.activated":    ("app.activated",    Priority.low),
}

_SUPPORTED_WIRE_VERSION = 1


class MacHelperBackend(BaseProducer):
    """Spawns the Swift helper and publishes its NDJSON stream to the bus."""

    name = "mac_helper"

    def __init__(self, bus: EventBus, helper_path: Path):
        self.bus = bus
        self.helper_path = helper_path
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._stream_loop())
        logger.info(
            "desktop_events: mac_helper backend started (binary=%s)",
            self.helper_path,
        )

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
        logger.info("desktop_events: mac_helper backend stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _stream_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    str(self.helper_path),
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
                        "desktop_events: mac_helper exited unexpectedly, "
                        "restarting in %.0fs",
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

            except asyncio.CancelledError:
                break
            except FileNotFoundError:
                logger.error(
                    "desktop_events: mac_helper binary not found at %s — "
                    "did the install move?",
                    self.helper_path,
                )
                break
            except Exception:
                logger.exception("desktop_events: mac_helper stream error")
                if self._running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    async def _handle_line(self, raw: bytes) -> None:
        stripped = raw.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            logger.debug("desktop_events: non-JSON helper output: %r", raw[:120])
            return

        if event.get("v") != _SUPPORTED_WIRE_VERSION:
            logger.warning(
                "desktop_events: unsupported helper wire version %r (expected %d)",
                event.get("v"),
                _SUPPORTED_WIRE_VERSION,
            )
            return

        event_type = event.get("type", "")

        if event_type == "helper.ready":
            logger.info("desktop_events: mac_helper handshake OK (%s)", event.get("payload"))
            return

        mapping = _TOPIC_MAP.get(event_type)
        if mapping is None:
            logger.debug("desktop_events: dropping unknown helper event %r", event_type)
            return

        topic, priority = mapping
        payload = event.get("payload") or {}
        # The helper emits ISO8601 timestamps; preserve them in the payload
        # so downstream consumers can distinguish helper-source time from
        # bus-arrival time.
        if "ts" in event:
            payload = {**payload, "source_ts": event["ts"]}

        await self.bus.publish(
            EventCreate(
                topic=topic,
                payload=payload,
                priority=priority,
                source="producer:mac_helper",
            ),
            producer_id="producer_mac_helper",
        )
