"""Policy engine — three-layer safety gate for agent-proposed actions.

Layer 1: Blocklist (compiled regex for dangerous patterns) → deny
Layer 2: Allowlist (safe read-only commands) → auto
Layer 3: Trust mode table (default trust per action type) → auto/confirm/deny
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

from aiventbus.models import TrustMode

logger = logging.getLogger(__name__)


class PolicyDecision(NamedTuple):
    trust_mode: TrustMode
    reason: str | None


# --- Layer 1: Blocklist (always deny) ---

_BLOCKLIST_PATTERNS = [
    r"rm\s+(-[rRf]+\s+)*/?(\s|$)",       # rm -rf / or rm /
    r"rm\s+.*--no-preserve-root",
    r"\bdd\s+if=",                         # dd if=
    r"\bmkfs\b",                           # mkfs
    r"\bchmod\s+777\b",                    # chmod 777
    r":\(\)\s*\{\s*:\|\:\s*&\s*\}\s*;",   # fork bomb
    r"\|\s*(curl|wget)\b",                 # pipe to curl/wget (exfiltration)
    r"(curl|wget).*\|.*\bsh\b",           # curl | sh
    r">\s*/dev/sd[a-z]",                   # write to block device
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\binit\s+[06]\b",
    r"\bsudo\b",                           # no sudo unless explicitly allowed
    r"\bsystemctl\s+(start|stop|restart|enable|disable|mask)\b",
    r"\biptables\b",
    r"\bufw\b",
    r"\bpasswd\b",
    r"\buseradd\b",
    r"\buserdel\b",
    r"\bchown\s+-R\s+root\b",
]

_BLOCKLIST_COMPILED = [re.compile(p, re.IGNORECASE) for p in _BLOCKLIST_PATTERNS]


# --- Layer 2: Allowlist (auto-approve for shell_exec) ---

_ALLOWLIST_COMMANDS = {
    # Read-only / informational
    "ls", "cat", "head", "tail", "wc", "df", "du", "ps", "uptime",
    "free", "date", "which", "file", "stat", "whoami", "hostname",
    "uname", "lsb_release", "env", "printenv", "id", "groups",
    "find", "locate", "tree", "less", "more",
    # Git (read-only)
    "git status", "git log", "git diff", "git branch", "git remote",
    "git show", "git tag",
    # Package info
    "pip list", "pip show", "pip freeze",
    "dpkg -l", "dpkg --list", "apt list",
    "pip3 list", "pip3 show",
    # System info
    "lscpu", "lspci", "lsusb", "lsblk", "nvidia-smi",
    "top -bn1", "vmstat", "iostat", "nproc",
}


# --- Layer 3: Default trust modes per action type ---

_DEFAULT_TRUST: dict[str, TrustMode] = {
    "emit_event": TrustMode.auto,
    "log": TrustMode.auto,
    "alert": TrustMode.auto,
    "notify": TrustMode.auto,
    "clipboard_set": TrustMode.auto,
    "file_read": TrustMode.auto,
    "get_knowledge": TrustMode.auto,
    "set_knowledge": TrustMode.auto,
    "shell_exec": TrustMode.confirm,
    "file_write": TrustMode.confirm,
    "file_delete": TrustMode.confirm,
    "open_app": TrustMode.confirm,
    "package_manage": TrustMode.deny,
    "systemctl": TrustMode.deny,
    "network": TrustMode.deny,
    "disk_format": TrustMode.deny,
}


def _check_blocklist(command: str) -> str | None:
    """Check if a command matches any blocklist pattern. Returns pattern if matched."""
    for pattern in _BLOCKLIST_COMPILED:
        if pattern.search(command):
            return pattern.pattern
    return None


def _check_allowlist(command: str) -> bool:
    """Check if a command starts with an allowlisted prefix."""
    cmd_stripped = command.strip()
    for safe_cmd in _ALLOWLIST_COMMANDS:
        if cmd_stripped == safe_cmd or cmd_stripped.startswith(safe_cmd + " "):
            return True
    return False


class PolicyEngine:
    """Evaluates proposed actions against safety policies."""

    def __init__(self, trust_overrides: dict[str, str] | None = None):
        self._trust_overrides: dict[str, TrustMode] = {}
        if trust_overrides:
            for action_type, mode in trust_overrides.items():
                try:
                    self._trust_overrides[action_type] = TrustMode(mode)
                except ValueError:
                    logger.warning("Invalid trust override: %s=%s", action_type, mode)

    def evaluate(self, action_type: str, action_data: dict) -> PolicyDecision:
        """Evaluate a proposed action through the three-layer policy stack.

        Returns a PolicyDecision with the resolved trust mode and reason.
        """
        # Layer 1: Blocklist (shell_exec only)
        if action_type == "shell_exec":
            command = action_data.get("command", "")
            blocked = _check_blocklist(command)
            if blocked:
                logger.warning("BLOCKED command: %s (pattern: %s)", command, blocked)
                return PolicyDecision(TrustMode.deny, f"Blocklisted pattern: {blocked}")

            # Layer 2: Allowlist
            if _check_allowlist(command):
                return PolicyDecision(TrustMode.auto, "Allowlisted safe command")

        # Layer 3: Trust mode table (with config overrides)
        if action_type in self._trust_overrides:
            mode = self._trust_overrides[action_type]
            return PolicyDecision(mode, f"Config override: {action_type}={mode.value}")

        default = _DEFAULT_TRUST.get(action_type, TrustMode.confirm)
        return PolicyDecision(default, None)
