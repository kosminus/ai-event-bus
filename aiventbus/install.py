"""Install / uninstall — generate systemd or launchd units from the live runtime.

The generated unit files always use ``sys.executable`` (so the daemon
launched by the service manager is the same Python as the one running
``aibus install``), and pin ``AIVENTBUS_CONFIG`` / ``AIVENTBUS_DB`` / —
on macOS — ``AIVENTBUS_MAC_HELPER`` into the service environment so the
daemon resolves identically from the service manager's minimal shell as
it does from an interactive CLI.

No templates are shipped in the repo; the unit text is rendered at
install time. That keeps re-running ``aibus install`` the right escape
hatch whenever the helper binary is moved, the venv changes, or the
user switches Python versions.
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from aiventbus import platform as _platform

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SYSTEMD_UNIT_NAME = "aiventbus.service"
LAUNCHD_LABEL = "com.aiventbus.daemon"
HELPER_BINARY_NAME = "aiventbus-mac-helper"


def _systemd_unit_dir() -> Path:
    """User-scope systemd unit directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "systemd" / "user"


def _launchd_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _swift_sources_dir() -> Path:
    """Repo-relative sources for the Swift helper (build-time only).

    Only used by ``--build-helper``; the runtime lookup of the *installed*
    helper is still strictly via ``$AIVENTBUS_MAC_HELPER`` or
    ``~/.local/bin/aiventbus-mac-helper`` (see ``platform.mac_helper_path``).

    Can be overridden with ``$AIVENTBUS_HELPER_SOURCES`` for unusual
    install layouts.
    """
    override = os.environ.get("AIVENTBUS_HELPER_SOURCES")
    if override:
        return Path(override).expanduser()
    # aiventbus/install.py → repo root is two levels up
    return Path(__file__).resolve().parent.parent / "bin" / HELPER_BINARY_NAME


# ---------------------------------------------------------------------------
# Default config.yaml bootstrap
# ---------------------------------------------------------------------------

_MINIMAL_CONFIG = textwrap.dedent(
    """\
    # aiventbus configuration. Written by `aibus install` on first run.
    # Docs: https://github.com/kosminus/ai-event-bus

    # database:
    #   path: "/absolute/override.db"  # optional override, resolver picks default otherwise

    producers:
      clipboard_enabled: true
      # journald_enabled: true   # enables system_log on Linux (journald) and macOS (log stream)
      # dbus_enabled: true       # enables desktop_events on Linux; needs `aibus install --build-helper` on macOS

    seed_defaults: true
    """
)


def _ensure_default_config(config_path: Path) -> bool:
    """Write a minimal config.yaml at ``config_path`` if it doesn't exist.

    Returns True if a new file was written, False otherwise.
    """
    if config_path.exists():
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_MINIMAL_CONFIG)
    return True


# ---------------------------------------------------------------------------
# Swift helper build
# ---------------------------------------------------------------------------

def build_mac_helper(*, dev: bool = False) -> Path:
    """Build ``aiventbus-mac-helper`` and install it to ``~/.local/bin``.

    In ``--dev`` mode the installed path is a symlink back to the repo
    build output so rebuilds are picked up immediately without re-running
    the installer.
    """
    if not _platform.IS_MACOS:
        raise RuntimeError("Swift helper is macOS-only")
    sources = _swift_sources_dir()
    if not (sources / "Package.swift").is_file():
        raise FileNotFoundError(
            f"Swift helper sources not found at {sources} "
            f"(override with $AIVENTBUS_HELPER_SOURCES)"
        )
    if not _platform.which("swift"):
        raise RuntimeError("`swift` not on PATH — install Xcode CLI tools first")

    logger.info("Building %s (release) at %s", HELPER_BINARY_NAME, sources)
    subprocess.run(
        ["swift", "build", "-c", "release"],
        cwd=sources,
        check=True,
    )
    built = sources / ".build" / "release" / HELPER_BINARY_NAME
    if not built.is_file():
        raise FileNotFoundError(f"Swift build produced no binary at {built}")

    install_dir = _platform.helper_install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)
    target = install_dir / HELPER_BINARY_NAME

    if target.exists() or target.is_symlink():
        target.unlink()

    if dev:
        target.symlink_to(built.resolve())
        logger.info("Linked %s -> %s", target, built.resolve())
    else:
        shutil.copy2(built, target)
        os.chmod(target, 0o755)
        logger.info("Installed %s", target)
    return target


# ---------------------------------------------------------------------------
# systemd user unit — Linux
# ---------------------------------------------------------------------------

def _render_systemd_unit(
    *,
    python_exe: str,
    config_path: Path,
    db_path: Path,
) -> str:
    # Written without leading whitespace so systemd reads every directive
    # at the start of its line (textwrap.dedent fights with the
    # independently-built env lines).
    return (
        "[Unit]\n"
        "Description=aiventbus daemon\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={python_exe} -m aiventbus\n"
        f"Environment=AIVENTBUS_CONFIG={config_path}\n"
        f"Environment=AIVENTBUS_DB={db_path}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _install_linux(*, dev: bool) -> None:
    config_path = _platform.default_config_path()
    db_path = _platform.default_db_path()
    _platform.ensure_runtime_dirs()
    wrote_config = _ensure_default_config(config_path)
    if wrote_config:
        logger.info("Wrote default config: %s", config_path)

    unit_dir = _systemd_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / SYSTEMD_UNIT_NAME

    body = _render_systemd_unit(
        python_exe=sys.executable,
        config_path=config_path,
        db_path=db_path,
    )
    unit_path.write_text(body)
    logger.info("Wrote systemd unit: %s", unit_path)

    if _platform.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME],
            check=False,
        )
        logger.info("Enabled systemd user unit: %s", SYSTEMD_UNIT_NAME)
    else:
        logger.warning(
            "systemctl not on PATH — unit written but not enabled. "
            "Run: systemctl --user daemon-reload && systemctl --user enable --now %s",
            SYSTEMD_UNIT_NAME,
        )


def _uninstall_linux(*, purge: bool) -> None:
    unit_path = _systemd_unit_dir() / SYSTEMD_UNIT_NAME
    if _platform.which("systemctl"):
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME],
            check=False,
        )
    if unit_path.exists():
        unit_path.unlink()
        logger.info("Removed systemd unit: %s", unit_path)
    if purge:
        _purge_dirs()


# ---------------------------------------------------------------------------
# launchd agent — macOS
# ---------------------------------------------------------------------------

def _render_launchd_plist(
    *,
    python_exe: str,
    config_path: Path,
    db_path: Path,
    helper_path: Path | None,
    stdout_log: Path,
    stderr_log: Path,
) -> bytes:
    env_vars: dict[str, str] = {
        "AIVENTBUS_CONFIG": str(config_path),
        "AIVENTBUS_DB":     str(db_path),
    }
    if helper_path is not None:
        env_vars["AIVENTBUS_MAC_HELPER"] = str(helper_path)

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [python_exe, "-m", "aiventbus"],
        "EnvironmentVariables": env_vars,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(stdout_log),
        "StandardErrorPath": str(stderr_log),
        "ProcessType": "Background",
    }
    return plistlib.dumps(plist)


def _install_macos(*, dev: bool, build_helper: bool) -> None:
    config_path = _platform.default_config_path()
    db_path = _platform.default_db_path()
    log_dir = _platform.log_dir()
    _platform.ensure_runtime_dirs()

    wrote_config = _ensure_default_config(config_path)
    if wrote_config:
        logger.info("Wrote default config: %s", config_path)

    helper_path: Path | None = None
    if build_helper:
        helper_path = build_mac_helper(dev=dev)
    else:
        helper_path = _platform.mac_helper_path()
        if helper_path is None:
            logger.info(
                "Swift helper not built yet — desktop_events will report unavailable "
                "until `aibus install --build-helper`"
            )

    launchd_dir = _launchd_dir()
    launchd_dir.mkdir(parents=True, exist_ok=True)
    plist_path = launchd_dir / f"{LAUNCHD_LABEL}.plist"

    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"

    body = _render_launchd_plist(
        python_exe=sys.executable,
        config_path=config_path,
        db_path=db_path,
        helper_path=helper_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
    plist_path.write_bytes(body)
    logger.info("Wrote launchd plist: %s", plist_path)

    if _platform.which("launchctl"):
        # Unload first in case the plist already loaded; ignore errors.
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
        subprocess.run(["launchctl", "load", str(plist_path)], check=False)
        logger.info("Loaded launchd agent: %s", LAUNCHD_LABEL)
    else:
        logger.warning(
            "launchctl not on PATH — plist written but not loaded. "
            "Run: launchctl load %s",
            plist_path,
        )


def _uninstall_macos(*, purge: bool) -> None:
    plist_path = _launchd_dir() / f"{LAUNCHD_LABEL}.plist"
    if _platform.which("launchctl") and plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
    if plist_path.exists():
        plist_path.unlink()
        logger.info("Removed launchd plist: %s", plist_path)

    helper_target = _platform.helper_install_dir() / HELPER_BINARY_NAME
    if helper_target.exists() or helper_target.is_symlink():
        helper_target.unlink()
        logger.info("Removed %s", helper_target)

    if purge:
        _purge_dirs()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _purge_dirs() -> None:
    """Delete data / config / log dirs. Only called when --purge is set."""
    for d in (_platform.config_dir(), _platform.data_dir(), _platform.log_dir()):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            logger.info("Purged %s", d)


def install(*, dev: bool = False, build_helper: bool = False) -> None:
    """Install the daemon for the current user.

    On Linux this writes a systemd user unit and enables it. On macOS it
    writes a LaunchAgent plist and loads it, optionally building and
    installing the Swift helper via ``build_helper=True``.
    """
    os_id = _platform.os_id()
    if os_id == "linux":
        _install_linux(dev=dev)
    elif os_id == "darwin":
        _install_macos(dev=dev, build_helper=build_helper)
    else:
        raise RuntimeError(f"`aibus install` is not supported on {os_id}")


def uninstall(*, purge: bool = False) -> None:
    """Remove the daemon installation."""
    os_id = _platform.os_id()
    if os_id == "linux":
        _uninstall_linux(purge=purge)
    elif os_id == "darwin":
        _uninstall_macos(purge=purge)
    else:
        raise RuntimeError(f"`aibus uninstall` is not supported on {os_id}")


# ---------------------------------------------------------------------------
# Restart — delegates to systemctl (Linux) or launchctl (macOS)
# ---------------------------------------------------------------------------

class NoServiceInstalled(RuntimeError):
    """Raised when `aibus restart` is called but no supervisor unit exists."""


def _uid() -> int:
    return os.getuid()


def _is_systemd_unit_active() -> bool:
    if not _platform.which("systemctl"):
        return False
    r = subprocess.run(
        ["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME],
        capture_output=True, text=True, check=False,
    )
    return r.returncode == 0 and r.stdout.strip() == "active"


def _is_systemd_unit_installed() -> bool:
    return (_systemd_unit_dir() / SYSTEMD_UNIT_NAME).is_file()


def _is_launchd_agent_loaded() -> bool:
    if not _platform.which("launchctl"):
        return False
    # `launchctl print gui/<uid>/<label>` returns 0 if the service exists in
    # that domain (loaded or not), non-zero otherwise. Cheaper than parsing
    # `launchctl list` output.
    r = subprocess.run(
        ["launchctl", "print", f"gui/{_uid()}/{LAUNCHD_LABEL}"],
        capture_output=True, check=False,
    )
    return r.returncode == 0


def _is_launchd_plist_present() -> bool:
    return (_launchd_dir() / f"{LAUNCHD_LABEL}.plist").is_file()


def restart() -> str:
    """Ask the service manager to restart the daemon.

    Returns a short human-readable string describing what happened,
    suitable for printing to the CLI. Raises ``NoServiceInstalled`` if
    the daemon isn't under a supervisor we can drive — in that case the
    user is running foreground and should Ctrl+C + relaunch themselves.
    """
    os_id = _platform.os_id()
    if os_id == "linux":
        if not _is_systemd_unit_installed():
            raise NoServiceInstalled(
                "No systemd unit found at "
                f"{_systemd_unit_dir() / SYSTEMD_UNIT_NAME} — "
                "run `aibus install` first, or Ctrl+C and relaunch manually."
            )
        if not _platform.which("systemctl"):
            raise RuntimeError("systemctl not on PATH")
        r = subprocess.run(
            ["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"systemctl restart failed (exit {r.returncode}): {r.stderr.strip() or r.stdout.strip()}"
            )
        return f"Restarted systemd unit: {SYSTEMD_UNIT_NAME}"

    if os_id == "darwin":
        if not _is_launchd_plist_present():
            raise NoServiceInstalled(
                "No LaunchAgent found at "
                f"{_launchd_dir() / (LAUNCHD_LABEL + '.plist')} — "
                "run `aibus install` first, or Ctrl+C and relaunch manually."
            )
        if not _platform.which("launchctl"):
            raise RuntimeError("launchctl not on PATH")
        target = f"gui/{_uid()}/{LAUNCHD_LABEL}"
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            # Most common failure: service not loaded. Try bootstrap + retry
            # once so the command is a useful "bring the thing up" hammer.
            plist = _launchd_dir() / f"{LAUNCHD_LABEL}.plist"
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{_uid()}", str(plist)],
                capture_output=True, check=False,
            )
            r = subprocess.run(
                ["launchctl", "kickstart", "-k", target],
                capture_output=True, text=True, check=False,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    "launchctl kickstart failed (exit "
                    f"{r.returncode}): {r.stderr.strip() or r.stdout.strip()}"
                )
        return f"Restarted LaunchAgent: {LAUNCHD_LABEL}"

    raise RuntimeError(f"`aibus restart` is not supported on {os_id}")
