"""journald backend — streams systemd journal entries via ``journalctl -f``.

Absorbs the Linux-specific implementation previously at
``aiventbus/producers/journald.py``. The producer shell, classification,
and priority gate are shared with the macOS backend in the package
``__init__``; only the command shape, the JSON schema, and the
OS-specific noise filter live here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Noisy identifiers to ignore by default (reduce event flood).
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

# Noisy unit patterns (slice / scope churn).
_NOISE_UNIT_PREFIXES = (
    "session-",
    "user@",
    "user-runtime-dir@",
    "run-",
    "snap.",
)


def _is_noisy(entry: dict[str, Any]) -> bool:
    ident = entry.get("SYSLOG_IDENTIFIER", "") or entry.get("_COMM", "")
    if ident in _NOISE_IDENTIFIERS:
        return True
    unit = entry.get("_SYSTEMD_UNIT", "")
    if any(unit.startswith(p) for p in _NOISE_UNIT_PREFIXES):
        return True
    return False


class JournaldBackend:
    """Linux backend: ``journalctl -f -o json``."""

    name = "journald"
    producer_source = "producer:journald"
    producer_id = "producer_journald"

    def __init__(self, filter_noise: bool = True, units: list[str] | None = None):
        self.filter_noise = filter_noise
        self.units = units or []

    def build_cmd(self) -> list[str]:
        """Build the journalctl command.

        Priority filtering is not done here — the shared producer handles
        it so auth/service entries always pass through regardless of
        their syslog priority. Unit filtering is applied here because it
        genuinely reduces journal read I/O.
        """
        cmd = ["journalctl", "-f", "-o", "json", "--no-pager"]
        for unit in self.units:
            cmd += ["-u", unit]
        return cmd

    def parse_line(self, raw: bytes) -> dict[str, Any] | None:
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if self.filter_noise and _is_noisy(entry):
            return None
        # journalctl already emits journal-compat keys; hand it back as-is
        # so the shared classifier can do its thing.
        return entry
