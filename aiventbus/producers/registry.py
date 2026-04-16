"""Producer registry.

Producers declare themselves once, against capabilities, not OSes. The
platform layer (``aiventbus.platform.capabilities``) reports which
capabilities exist on this machine; ``ProducerManager`` consults the
registry + the platform layer to decide what to expose, what to start by
default, and which capabilities to advertise as unavailable in the API
response.

Adding a producer means appending one ``ProducerSpec`` to ``REGISTRY``.
Adding a new OS backend for an existing producer means adding a branch
inside the producer's factory — never inside the registry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from aiventbus import platform as _platform
from aiventbus.config import AppConfig
from aiventbus.core.bus import EventBus
from aiventbus.platform import Capability
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spec dataclass — one entry per user-facing producer.
# ---------------------------------------------------------------------------

ProducerFactory = Callable[[EventBus, AppConfig], BaseProducer | None]
EnabledPredicate = Callable[[AppConfig], bool]


@dataclass(frozen=True)
class ProducerSpec:
    """Declarative description of a producer.

    ``capabilities`` is the list of capabilities the producer *may* provide.
    A producer is considered runnable if **at least one** listed capability
    is available on this machine; the producer itself is responsible for
    skipping publishes for capabilities it can't back. ``required_capabilities``
    (optional) are capabilities that must all be available for the producer
    to start at all.

    ``supported_platforms`` lists the OS identifiers on which this codebase
    has a working backend for the producer. ``None`` means "any OS where the
    capabilities resolve." Values are the short names returned by
    ``aiventbus.platform.os_id``: ``"linux"``, ``"darwin"``, ``"windows"``.
    When the current OS is absent from the set, the producer is advertised
    as unavailable with a "Not implemented on <OS> yet" reason so the UI
    never shows an Enable button for something that cannot start.
    """

    name: str
    description: str
    topics: list[str]
    capabilities: list[Capability]
    factory: ProducerFactory
    is_enabled: EnabledPredicate
    required_capabilities: list[Capability] = field(default_factory=list)
    supported_platforms: set[str] | None = None


# ---------------------------------------------------------------------------
# Factories — one per producer. Each factory is responsible for:
#   1. returning ``None`` if configuration disables it or makes it impossible
#      to construct (e.g. no paths configured),
#   2. otherwise constructing the producer with OS-aware backends.
#
# Factories are kept as lightweight thunks so the registry never imports
# producer modules eagerly (especially important for the DBus-backed
# producer, which should never be imported on macOS).
# ---------------------------------------------------------------------------

def _build_clipboard(bus: EventBus, cfg: AppConfig) -> BaseProducer | None:
    from aiventbus.producers.clipboard import ClipboardProducer

    return ClipboardProducer(
        bus=bus,
        poll_interval_ms=cfg.producers.clipboard_poll_interval_ms,
        min_length=cfg.producers.clipboard_min_length,
    )


def _build_file_watcher(bus: EventBus, cfg: AppConfig) -> BaseProducer | None:
    from aiventbus.producers.file_watcher import FileWatcherProducer

    if not cfg.producers.file_watcher_paths:
        return None
    return FileWatcherProducer(bus=bus, watch_paths=cfg.producers.file_watcher_paths)


def _build_terminal_monitor(bus: EventBus, cfg: AppConfig) -> BaseProducer | None:
    from aiventbus.producers.terminal_monitor import TerminalMonitorProducer

    return TerminalMonitorProducer(
        bus=bus,
        history_path=cfg.producers.terminal_history_path,
    )


def _build_webhook(bus: EventBus, cfg: AppConfig) -> BaseProducer | None:
    from aiventbus.producers.webhook import WebhookProducer

    return WebhookProducer(
        bus=bus,
        secret=cfg.producers.webhook_secret,
        default_priority=cfg.producers.webhook_default_priority,
    )


def _build_cron(bus: EventBus, cfg: AppConfig) -> BaseProducer | None:
    from aiventbus.producers.cron import CronProducer

    return CronProducer(
        bus=bus,
        jobs=cfg.producers.cron_jobs,
        timezone=cfg.producers.cron_timezone,
    )


def _build_system_log(bus: EventBus, cfg: AppConfig) -> BaseProducer | None:
    """System log producer (journald on Linux, log stream on macOS).

    Until PR 4 lands the unified ``system_log`` package, we keep the
    existing journald implementation as the Linux backend and return
    ``None`` on other OSes so the registry marks the producer unavailable.
    """
    if _platform.IS_LINUX:
        from aiventbus.producers.journald import JournaldProducer

        return JournaldProducer(
            bus=bus,
            filter_noise=cfg.producers.journald_filter_noise,
            priority_filter=cfg.producers.journald_priority_filter,
            units=cfg.producers.journald_units,
        )
    # macOS backend is introduced in PR 4. Until then, producing no instance
    # is the right thing to do — ProducerManager will report this producer
    # as unavailable with the system_log capability's reason from the
    # platform layer.
    return None


def _build_desktop_events(bus: EventBus, cfg: AppConfig) -> BaseProducer | None:
    """Desktop events producer (DBus on Linux, Swift helper on macOS).

    PR 5 will unify the Linux and macOS backends under a single
    ``desktop_events`` package. Until then, we dispatch to the existing
    ``dbus_listener`` on Linux and return ``None`` elsewhere.
    """
    if _platform.IS_LINUX:
        from aiventbus.producers.dbus_listener import DBusListenerProducer

        return DBusListenerProducer(bus=bus)
    return None


# ---------------------------------------------------------------------------
# Registry — the single source of truth for what producers exist.
# ---------------------------------------------------------------------------

REGISTRY: list[ProducerSpec] = [
    ProducerSpec(
        name="clipboard",
        description="Watches the system clipboard for new text (pbpaste / xclip / wl-paste).",
        topics=["clipboard.text"],
        capabilities=[Capability.CLIPBOARD],
        required_capabilities=[Capability.CLIPBOARD],
        factory=_build_clipboard,
        is_enabled=lambda cfg: cfg.producers.clipboard_enabled,
    ),
    ProducerSpec(
        name="file_watcher",
        description="Watches directories for file create / modify / delete events.",
        topics=["fs.created", "fs.modified", "fs.deleted"],
        capabilities=[Capability.FILE_WATCH],
        required_capabilities=[Capability.FILE_WATCH],
        factory=_build_file_watcher,
        is_enabled=lambda cfg: cfg.producers.file_watcher_enabled
        and bool(cfg.producers.file_watcher_paths),
    ),
    ProducerSpec(
        name="terminal_monitor",
        description="Polls shell history for new commands (bash / zsh).",
        topics=["terminal.command"],
        capabilities=[Capability.TERMINAL_HISTORY],
        required_capabilities=[Capability.TERMINAL_HISTORY],
        factory=_build_terminal_monitor,
        is_enabled=lambda cfg: cfg.producers.terminal_monitor_enabled,
    ),
    ProducerSpec(
        name="webhook",
        description="Receives HTTP POST webhooks and publishes them as bus events.",
        topics=["webhook.*"],
        capabilities=[Capability.WEBHOOK],
        required_capabilities=[Capability.WEBHOOK],
        factory=_build_webhook,
        is_enabled=lambda cfg: cfg.producers.webhook_enabled,
    ),
    ProducerSpec(
        name="cron",
        description="Publishes events on a cron schedule or at fixed intervals.",
        topics=["cron.*"],
        capabilities=[Capability.CRON],
        required_capabilities=[Capability.CRON],
        factory=_build_cron,
        is_enabled=lambda cfg: cfg.producers.cron_enabled,
    ),
    ProducerSpec(
        name="system_log",
        description="Streams system-log entries (journald on Linux, log stream on macOS).",
        topics=["syslog.error", "syslog.warning", "syslog.auth", "syslog.service", "syslog.info"],
        capabilities=[Capability.SYSTEM_LOG],
        required_capabilities=[Capability.SYSTEM_LOG],
        factory=_build_system_log,
        is_enabled=lambda cfg: cfg.producers.journald_enabled,
        # PR 4 adds the macOS log_stream backend. Until then the spec is
        # honestly Linux-only, even though the SYSTEM_LOG capability reports
        # available on macOS (the `log` binary exists; nothing in this
        # codebase consumes it yet).
        supported_platforms={"linux"},
    ),
    ProducerSpec(
        name="desktop_events",
        description=(
            "Desktop signals: screen lock / unlock, app launch / quit / activate, "
            "and (where supported) inbound desktop notifications."
        ),
        topics=[
            "session.locked", "session.unlocked",
            "app.launched", "app.terminated", "app.activated",
            "notification.received",
        ],
        capabilities=[
            Capability.SESSION_STATE,
            Capability.APP_LIFECYCLE,
            Capability.NOTIFICATIONS_RECEIVED,
        ],
        # No required capabilities — the producer starts as long as at least
        # one listed capability is available, and publishes only the topics
        # whose backing capability is available.
        required_capabilities=[],
        factory=_build_desktop_events,
        is_enabled=lambda cfg: cfg.producers.dbus_enabled,
        # PR 5 introduces the Swift sidecar + macOS backend. Until then,
        # even if someone manually points ``$AIVENTBUS_MAC_HELPER`` at a
        # stub binary and flips SESSION_STATE to available, the producer
        # can't run — so gate explicitly.
        supported_platforms={"linux"},
    ),
]


def get_spec(name: str) -> ProducerSpec | None:
    """Return the spec for a producer name, or ``None`` if unknown."""
    for spec in REGISTRY:
        if spec.name == name:
            return spec
    return None
