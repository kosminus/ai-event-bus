"""Platform boundary — single source of truth for OS-specific behavior.

The rest of the codebase never imports ``sys.platform`` or calls ``shutil.which``
directly. Everything goes through this module, which exposes:

- Directory paths (config / data / log / runtime / helper install).
- Deterministic helper binary lookup.
- Executable-probing helpers (``which``).
- Backend factories for the few places that need to run an OS-specific command
  (notifications, app-opening, clipboard).
- A capability enum describing what this machine can do, independent of OS
  names.
- ``platform_facts()`` for the system-facts seeder and agent prompt
  generation.

Adding a new OS means adding branches here — not scattering ``sys.platform``
checks across the codebase.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import platform as _platform
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# OS flags — internal. Callers should prefer capability queries below.
# ---------------------------------------------------------------------------

IS_LINUX: bool = sys.platform.startswith("linux")
IS_MACOS: bool = sys.platform == "darwin"
IS_WINDOWS: bool = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Directories — one place defines where state lives on each OS.
# ---------------------------------------------------------------------------

_APP_NAME = "aiventbus"


def _home() -> Path:
    return Path.home()


def config_dir() -> Path:
    """Per-user config directory."""
    if IS_MACOS:
        return _home() / "Library" / "Application Support" / _APP_NAME
    # Linux / other unix: honour XDG_CONFIG_HOME if set.
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / _APP_NAME


def data_dir() -> Path:
    """Per-user data directory (DB, long-lived state)."""
    if IS_MACOS:
        return _home() / "Library" / "Application Support" / _APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(_home() / ".local" / "share")
    return Path(base) / _APP_NAME


def log_dir() -> Path:
    """Per-user log directory."""
    if IS_MACOS:
        return _home() / "Library" / "Logs" / _APP_NAME
    base = os.environ.get("XDG_STATE_HOME") or str(_home() / ".local" / "state")
    return Path(base) / _APP_NAME / "logs"


def runtime_dir() -> Path:
    """Per-user runtime directory (sockets, pid files, caches)."""
    if IS_MACOS:
        return _home() / "Library" / "Caches" / _APP_NAME
    base = os.environ.get("XDG_RUNTIME_DIR") or str(_home() / ".local" / "run")
    return Path(base) / _APP_NAME


def default_config_path() -> Path:
    return config_dir() / "config.yaml"


def default_db_path() -> Path:
    return data_dir() / f"{_APP_NAME}.db"


def helper_install_dir() -> Path:
    """Stable location where ``aibus install --build-helper`` copies the
    macOS helper binary. ``~/.local/bin`` on both Linux and macOS keeps the
    user-site convention consistent.
    """
    return _home() / ".local" / "bin"


def ensure_runtime_dirs() -> None:
    """Create config / data / log dirs if they don't already exist."""
    for d in (config_dir(), data_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Executable discovery — nothing outside this module calls shutil.which.
# ---------------------------------------------------------------------------

def which(name: str) -> Path | None:
    """Locate an executable on ``$PATH``. Returns ``None`` if not found."""
    found = shutil.which(name)
    return Path(found) if found else None


# ---------------------------------------------------------------------------
# Helper binary (macOS Swift sidecar) — deterministic lookup.
#
# The contract is strict on purpose: a daemon launched by launchd with a
# minimal environment must resolve the helper identically to a CLI run from
# a repo checkout. Neither a ``$PATH`` scan nor a repo-relative fallback is
# performed.
# ---------------------------------------------------------------------------

_HELPER_BINARY_NAME = "aiventbus-mac-helper"


def mac_helper_path() -> Path | None:
    """Return the absolute path to the macOS helper binary, or ``None``.

    Lookup order:
        1. ``$AIVENTBUS_MAC_HELPER`` env var (used by the generated launchd
           plist to pin a resolved absolute path).
        2. ``helper_install_dir() / "aiventbus-mac-helper"`` — where
           ``aibus install --build-helper`` places the compiled binary.
    """
    override = os.environ.get("AIVENTBUS_MAC_HELPER")
    if override:
        p = Path(override)
        return p if p.is_file() and os.access(p, os.X_OK) else None

    installed = helper_install_dir() / _HELPER_BINARY_NAME
    if installed.is_file() and os.access(installed, os.X_OK):
        return installed
    return None


# ---------------------------------------------------------------------------
# OS-specific command builders.
#
# Each backend wraps the escaping + command shape for one OS tool. The
# executor and producers hold a reference to one of these rather than
# branching on the OS themselves.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Notifier:
    backend: str  # "notify-send" | "osascript"
    executable: Path

    def build_command(self, title: str, message: str) -> list[str]:
        if self.backend == "osascript":
            # AppleScript requires \ and " escaped inside the string literal.
            def _esc(s: str) -> str:
                return s.replace("\\", "\\\\").replace('"', '\\"')

            script = (
                f'display notification "{_esc(message)}" '
                f'with title "{_esc(title)}"'
            )
            return [str(self.executable), "-e", script]
        # notify-send and anything else that takes positional title + body.
        return [str(self.executable), title, message]


@dataclass(frozen=True)
class Opener:
    backend: str  # "xdg-open" | "open"
    executable: Path

    def build_command(self, target: str) -> list[str]:
        return [str(self.executable), target]


@dataclass(frozen=True)
class ClipboardBackend:
    backend: str  # "pbpaste" | "xclip" | "wl-paste"
    executable: Path

    def read_command(self) -> list[str]:
        if self.backend == "xclip":
            return [str(self.executable), "-selection", "clipboard", "-o"]
        if self.backend == "wl-paste":
            return [str(self.executable), "--no-newline", "--type", "text/plain"]
        # pbpaste takes no arguments.
        return [str(self.executable)]


def notifier() -> Notifier | None:
    """Return a notifier for outbound desktop notifications, or ``None``."""
    if IS_MACOS:
        exe = which("osascript")
        if exe:
            return Notifier(backend="osascript", executable=exe)
        return None
    if IS_LINUX:
        exe = which("notify-send")
        if exe:
            return Notifier(backend="notify-send", executable=exe)
        return None
    return None


def opener() -> Opener | None:
    """Return an opener for URLs / files / applications, or ``None``."""
    if IS_MACOS:
        exe = which("open")
        if exe:
            return Opener(backend="open", executable=exe)
        return None
    if IS_LINUX:
        exe = which("xdg-open")
        if exe:
            return Opener(backend="xdg-open", executable=exe)
        return None
    return None


def clipboard_backend() -> ClipboardBackend | None:
    """Return a clipboard read backend, or ``None`` if none is available."""
    if IS_MACOS:
        exe = which("pbpaste")
        if exe:
            return ClipboardBackend(backend="pbpaste", executable=exe)
        return None
    if IS_LINUX:
        exe = which("xclip")
        if exe:
            return ClipboardBackend(backend="xclip", executable=exe)
        exe = which("wl-paste")
        if exe:
            return ClipboardBackend(backend="wl-paste", executable=exe)
        return None
    return None


# ---------------------------------------------------------------------------
# Capabilities — producers + executor + UI ask these, never sys.platform.
# ---------------------------------------------------------------------------

class Capability(StrEnum):
    CLIPBOARD = "clipboard"
    SYSTEM_LOG = "system_log"
    SESSION_STATE = "session_state"
    APP_LIFECYCLE = "app_lifecycle"
    NOTIFICATIONS_RECEIVED = "notifications_received"
    NOTIFICATIONS_OUTBOUND = "notifications_outbound"
    FILE_WATCH = "file_watch"
    TERMINAL_HISTORY = "terminal_history"
    SHELL_HOOK = "shell_hook"
    WEBHOOK = "webhook"
    CRON = "cron"


@dataclass(frozen=True)
class CapabilityStatus:
    available: bool
    backend: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "backend": self.backend,
            "reason": self.reason,
        }


def _cap_clipboard() -> CapabilityStatus:
    cb = clipboard_backend()
    if cb:
        return CapabilityStatus(True, backend=cb.backend)
    if IS_LINUX:
        return CapabilityStatus(False, reason="Install xclip or wl-clipboard")
    if IS_MACOS:
        return CapabilityStatus(False, reason="pbpaste not found (should ship with macOS)")
    return CapabilityStatus(False, reason="No clipboard backend on this OS")


def _cap_system_log() -> CapabilityStatus:
    if IS_LINUX:
        if which("journalctl"):
            return CapabilityStatus(True, backend="journalctl")
        return CapabilityStatus(False, reason="journalctl not found (systemd not installed?)")
    if IS_MACOS:
        if which("log"):
            return CapabilityStatus(True, backend="log_stream")
        return CapabilityStatus(False, reason="`log` command not found")
    return CapabilityStatus(False, reason="No system-log backend on this OS")


def _cap_session_state() -> CapabilityStatus:
    if IS_LINUX:
        # DBus presence is checked by the producer at start-up; we optimistically
        # assume a graphical Linux session has DBus.
        if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return CapabilityStatus(True, backend="dbus_fast")
        return CapabilityStatus(False, reason="No DBus session bus available")
    if IS_MACOS:
        if mac_helper_path() is not None:
            return CapabilityStatus(True, backend="mac_helper")
        return CapabilityStatus(
            False,
            reason="macOS helper not installed — run: aibus install --build-helper",
        )
    return CapabilityStatus(False, reason="No session-state backend on this OS")


def _cap_app_lifecycle() -> CapabilityStatus:
    if IS_MACOS:
        if mac_helper_path() is not None:
            return CapabilityStatus(True, backend="mac_helper")
        return CapabilityStatus(
            False,
            reason="macOS helper not installed — run: aibus install --build-helper",
        )
    # DBus signals for app launch/quit are inconsistent across Linux DEs.
    return CapabilityStatus(False, reason="App lifecycle capture not implemented on this OS")


def _cap_notifications_received() -> CapabilityStatus:
    if IS_LINUX:
        if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return CapabilityStatus(True, backend="dbus_fast")
        return CapabilityStatus(False, reason="No DBus session bus available")
    if IS_MACOS:
        return CapabilityStatus(
            False,
            reason="Not supported on macOS without a system extension",
        )
    return CapabilityStatus(False, reason="Not supported on this OS")


def _cap_notifications_outbound() -> CapabilityStatus:
    n = notifier()
    if n:
        return CapabilityStatus(True, backend=n.backend)
    if IS_LINUX:
        return CapabilityStatus(False, reason="Install libnotify-bin (notify-send)")
    if IS_MACOS:
        return CapabilityStatus(False, reason="osascript not found")
    return CapabilityStatus(False, reason="No notifier on this OS")


def capabilities() -> dict[Capability, CapabilityStatus]:
    """Report what this machine can do right now."""
    return {
        Capability.CLIPBOARD: _cap_clipboard(),
        Capability.SYSTEM_LOG: _cap_system_log(),
        Capability.SESSION_STATE: _cap_session_state(),
        Capability.APP_LIFECYCLE: _cap_app_lifecycle(),
        Capability.NOTIFICATIONS_RECEIVED: _cap_notifications_received(),
        Capability.NOTIFICATIONS_OUTBOUND: _cap_notifications_outbound(),
        # The four producers below are portable; they have no OS gating.
        Capability.FILE_WATCH: CapabilityStatus(True, backend="watchfiles"),
        Capability.TERMINAL_HISTORY: CapabilityStatus(True, backend="history_file"),
        Capability.SHELL_HOOK: CapabilityStatus(True, backend="preexec"),
        Capability.WEBHOOK: CapabilityStatus(True, backend="http"),
        Capability.CRON: CapabilityStatus(True, backend="apscheduler"),
    }


# ---------------------------------------------------------------------------
# Platform facts — for the system-facts seeder and agent prompt generation.
# ---------------------------------------------------------------------------

def _os_name() -> str:
    if IS_MACOS:
        return "macOS"
    if IS_LINUX:
        return "Linux"
    if IS_WINDOWS:
        return "Windows"
    return sys.platform


def _os_version() -> str:
    if IS_MACOS:
        version = _platform.mac_ver()[0]
        return version or "unknown"
    if IS_LINUX:
        # Try /etc/os-release; fall back to platform.platform().
        try:
            with open("/etc/os-release") as f:
                kv: dict[str, str] = {}
                for line in f:
                    if "=" in line:
                        k, v = line.rstrip().split("=", 1)
                        kv[k] = v.strip('"')
            name = kv.get("PRETTY_NAME") or kv.get("NAME") or _platform.platform()
            return name
        except (FileNotFoundError, OSError):
            return _platform.platform()
    return _platform.platform()


def _memory_gb() -> float | None:
    try:
        if IS_MACOS:
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], stderr=subprocess.DEVNULL, text=True
            ).strip()
            return round(int(out) / (1024 ** 3), 1)
        if IS_LINUX:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024 ** 2), 1)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None
    return None


def _gpu_summary() -> str | None:
    # Try nvidia-smi first — works wherever NVIDIA drivers exist (Linux most
    # commonly).
    nvidia = which("nvidia-smi")
    if nvidia:
        try:
            out = subprocess.check_output(
                [str(nvidia), "--query-gpu=name", "--format=csv,noheader"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            ).strip()
            if out:
                return ", ".join(line.strip() for line in out.splitlines() if line.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    if IS_MACOS:
        sp = which("system_profiler")
        if sp:
            try:
                raw = subprocess.check_output(
                    [str(sp), "-json", "SPDisplaysDataType"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=10,
                )
                data = json.loads(raw)
                items = data.get("SPDisplaysDataType", [])
                models = [it.get("sppci_model") or it.get("_name") for it in items]
                models = [m for m in models if m]
                if models:
                    return ", ".join(models)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                pass
    return None


def _shell_name() -> str:
    shell = os.environ.get("SHELL") or ""
    return Path(shell).name if shell else "unknown"


def _log_tools() -> str:
    if IS_LINUX:
        return "journalctl / systemctl"
    if IS_MACOS:
        return "log show / launchctl"
    return "unknown"


def _desktop_env() -> str:
    if IS_MACOS:
        return "Aqua"
    return os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "unknown"


def platform_facts() -> dict[str, Any]:
    """Collect facts used by the seeder and agent prompt generation.

    All values degrade to sensible defaults on failure — this function never
    raises.
    """
    return {
        "hostname": socket.gethostname(),
        "username": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "os_name": _os_name(),
        "os_version": _os_version(),
        "kernel": _platform.release(),
        "arch": _platform.machine(),
        "python": _platform.python_version(),
        "shell": _shell_name(),
        "memory_gb": _memory_gb(),
        "gpu": _gpu_summary(),
        "log_tools": _log_tools(),
        "desktop_env": _desktop_env(),
    }
