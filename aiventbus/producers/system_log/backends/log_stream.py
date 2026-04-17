"""macOS backend — streams unified-logging entries via ``log stream``.

``log stream --style=ndjson`` prints one JSON object per line; the schema
is documented at ``man log`` (the ``show``/``stream`` subcommand share it).
Typical fields we care about::

    {
      "timestamp": "2026-04-17 02:50:04.123456-0700",
      "messageType": "default",      # default | info | debug | error | fault
      "eventType": "logEvent",       # logEvent | activityCreate | stateEvent | ...
      "subsystem": "com.apple.foo",
      "category": "bar",
      "processID": 1234,
      "processImagePath": "/usr/bin/sshd",
      "senderImagePath": "/usr/lib/...",
      "eventMessage": "the actual message"
    }

We normalize that into the journal-compatible shape that the shared
classifier understands, applying an Apple-specific noise filter and a
default predicate so we don't drink from the firehose.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Apple's message type → syslog priority (0-7). Keeps the classifier
# portable: a "fault" on macOS classifies as error, a "default" message
# as notice (5), etc.
_MESSAGE_TYPE_TO_PRIORITY = {
    "fault":   2,  # crit
    "error":   3,  # err
    "default": 5,  # notice — macOS emits most app logs at this level
    "info":    6,
    "debug":   7,
}

# Sensible default predicate: surface anything error- or fault-level plus
# categories we care about for auth. Filter out `debug`/`info` — they're
# a firehose.
_DEFAULT_PREDICATE = (
    'messageType == error OR messageType == fault '
    'OR subsystem == "com.apple.authd" '
    'OR subsystem == "com.apple.opendirectoryd" '
    'OR subsystem == "com.apple.securityd"'
)

# Noisy process basenames and subsystems on macOS. These are high-volume
# background services whose log lines almost never carry user-relevant
# signal.
_NOISE_PROCESSES = {
    "mdworker_shared",
    "mdworker",
    "corespotlightd",
    "Spotlight",
    "logd",
    "cloudd",
    "bird",          # iCloud sync daemon
    "tccd",
    "fseventsd",
}

_NOISE_SUBSYSTEMS = {
    "com.apple.mdworker",
    "com.apple.Spotlight",
    "com.apple.CoreBrightness",
    "com.apple.bluetooth",
    "com.apple.WirelessDiagnostics",
    "com.apple.runningboard",
}


def _is_noisy(process: str, subsystem: str) -> bool:
    if process in _NOISE_PROCESSES:
        return True
    if subsystem in _NOISE_SUBSYSTEMS:
        return True
    return False


class LogStreamBackend:
    """macOS backend: ``log stream --style=ndjson --predicate <...>``."""

    name = "log_stream"
    producer_source = "producer:log_stream"
    producer_id = "producer_log_stream"

    def __init__(
        self,
        filter_noise: bool = True,
        predicate_override: str | None = None,
    ):
        self.filter_noise = filter_noise
        self.predicate = predicate_override or _DEFAULT_PREDICATE

    def build_cmd(self) -> list[str]:
        # ``--no-backtrace`` drops per-entry crash trace blobs that blow
        # up the ndjson line size. ``--info`` and ``--debug`` are
        # intentionally omitted — if the user wants them they can
        # override the predicate.
        return [
            "log", "stream",
            "--style", "ndjson",
            "--predicate", self.predicate,
        ]

    def parse_line(self, raw: bytes) -> dict[str, Any] | None:
        # Skip the header line that ``log stream`` emits before the first
        # event (``Filtering the log data using "..."``).
        stripped = raw.strip()
        if not stripped or not stripped.startswith(b"{"):
            return None
        try:
            src = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None

        # Skip activity-create and state events — they're lifecycle markers
        # without a log message body and would pollute the bus.
        event_type = src.get("eventType", "") or ""
        if event_type and event_type != "logEvent":
            return None

        process_path = src.get("processImagePath", "") or ""
        process_name = Path(process_path).name if process_path else ""
        subsystem = src.get("subsystem", "") or ""

        if self.filter_noise and _is_noisy(process_name, subsystem):
            return None

        message = src.get("eventMessage", "") or ""
        # macOS emits ``messageType`` capitalized ("Default", "Error",
        # "Fault", "Info", "Debug"); our priority map is lowercase.
        message_type_raw = src.get("messageType", "default") or "default"
        message_type = message_type_raw.lower()
        priority = _MESSAGE_TYPE_TO_PRIORITY.get(message_type, 6)

        # Return the journal-compat dict the shared classifier consumes.
        return {
            "MESSAGE": message,
            "PRIORITY": priority,
            "SYSLOG_IDENTIFIER": process_name,
            "_COMM": process_name,
            "_SYSTEMD_UNIT": "",          # no direct equivalent on macOS
            "_PID": src.get("processID", ""),
            "SYSLOG_FACILITY": "",        # not exposed by unified logging
            "_SUBSYSTEM": subsystem,
            "_CATEGORY": src.get("category", "") or "",
            "_MESSAGE_TYPE": message_type,
        }
