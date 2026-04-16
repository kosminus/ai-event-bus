"""Producer manager — enumerates the registry and gates on capabilities.

All "which producers exist" knowledge lives in ``aiventbus.producers.registry``;
all "what can this machine do" knowledge lives in ``aiventbus.platform``. The
manager is the glue: it walks the registry, asks the platform layer for each
spec's capability statuses, instantiates what's available, and surfaces the
rest with concrete ``reason`` strings via ``list_all``.
"""

from __future__ import annotations

import logging
from typing import Any

from aiventbus import platform as _platform
from aiventbus.core.bus import EventBus
from aiventbus.platform import Capability, CapabilityStatus
from aiventbus.producers.base import BaseProducer
from aiventbus.producers.registry import REGISTRY, ProducerSpec, get_spec

logger = logging.getLogger(__name__)


class ProducerManager:
    """Manages the lifecycle of event producers."""

    def __init__(self, bus: EventBus, config):
        self.bus = bus
        self.config = config
        self._producers: dict[str, BaseProducer] = {}

    # ------------------------------------------------------------------
    # Capability helpers
    # ------------------------------------------------------------------

    def _caps(self) -> dict[Capability, CapabilityStatus]:
        return _platform.capabilities()

    def _spec_capability_map(
        self, spec: ProducerSpec
    ) -> dict[str, dict[str, Any]]:
        """Return the capability status map for a spec, keyed by capability name."""
        all_caps = self._caps()
        return {
            cap.value: all_caps.get(cap, CapabilityStatus(False, reason="Unknown capability")).to_dict()
            for cap in spec.capabilities
        }

    def _runnable_reason(self, spec: ProducerSpec) -> str | None:
        """Return ``None`` if the spec is runnable on this machine, else a reason."""
        all_caps = self._caps()

        # Required capabilities: all must be available.
        for cap in spec.required_capabilities:
            status = all_caps.get(cap)
            if status is None or not status.available:
                return (
                    status.reason
                    if status and status.reason
                    else f"Required capability '{cap.value}' unavailable"
                )

        # If no required capabilities, at least one listed capability must
        # be available (otherwise the producer has nothing to do).
        if not spec.required_capabilities and spec.capabilities:
            any_available = any(
                all_caps.get(cap) and all_caps[cap].available for cap in spec.capabilities
            )
            if not any_available:
                reasons = [
                    f"{cap.value}: {all_caps[cap].reason}"
                    for cap in spec.capabilities
                    if all_caps.get(cap) and all_caps[cap].reason
                ]
                return "; ".join(reasons) or "No backing capability available on this platform"

        return None

    # ------------------------------------------------------------------
    # Creation + lifecycle
    # ------------------------------------------------------------------

    def _create_producer(self, spec: ProducerSpec) -> BaseProducer | None:
        """Instantiate a producer from its spec. Never raises."""
        try:
            return spec.factory(self.bus, self.config)
        except Exception:
            logger.exception("Producer factory failed for %s", spec.name)
            return None

    async def start_all(self) -> None:
        """Start every registered producer that is both enabled in config
        and runnable on this machine.
        """
        started = 0
        for spec in REGISTRY:
            if not spec.is_enabled(self.config):
                continue
            reason = self._runnable_reason(spec)
            if reason is not None:
                logger.info("Skipping producer %s: %s", spec.name, reason)
                continue
            if await self.enable(spec.name):
                started += 1
        logger.info("Started %d producers", started)

    async def stop_all(self) -> None:
        for name in list(self._producers):
            await self.disable(name)

    async def enable(self, name: str) -> bool:
        """Start a producer by name. Returns True on success, False if the
        producer is unknown, disabled by capability, or already running.
        """
        spec = get_spec(name)
        if spec is None:
            logger.warning("Cannot enable unknown producer: %s", name)
            return False

        existing = self._producers.get(name)
        if existing and existing.is_running:
            return True

        reason = self._runnable_reason(spec)
        if reason is not None:
            logger.warning("Cannot enable producer %s: %s", name, reason)
            return False

        producer = self._create_producer(spec)
        if producer is None:
            logger.warning("Producer factory returned None: %s", name)
            return False
        self._producers[name] = producer
        try:
            await producer.start()
        except Exception:
            logger.exception("Producer %s failed to start", name)
            self._producers.pop(name, None)
            return False
        logger.info("Producer enabled: %s", name)
        return True

    async def disable(self, name: str) -> bool:
        producer = self._producers.pop(name, None)
        if producer and producer.is_running:
            try:
                await producer.stop()
            except Exception:
                logger.exception("Producer %s failed to stop cleanly", name)
            logger.info("Producer disabled: %s", name)
            return True
        return False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get(self, name: str) -> BaseProducer | None:
        return self._producers.get(name)

    def list_running(self) -> list[str]:
        return [name for name, p in self._producers.items() if p.is_running]

    def list_all(self) -> list[dict[str, Any]]:
        """Return the full producer catalogue with per-capability status."""
        result: list[dict[str, Any]] = []
        for spec in REGISTRY:
            producer = self._producers.get(spec.name)
            reason = self._runnable_reason(spec)
            result.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "topics": list(spec.topics),
                    "running": bool(producer and producer.is_running),
                    "available": reason is None,
                    "unavailable_reason": reason,
                    "enabled_in_config": spec.is_enabled(self.config),
                    "capabilities": self._spec_capability_map(spec),
                }
            )
        return result
