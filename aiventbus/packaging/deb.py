"""Debian package builder for the PyInstaller-bundled daemon.

The produced ``.deb`` is self-contained: the bundled Python interpreter, all
third-party dependencies, and the aiventbus code live under ``/opt/aiventbus``.
No ``pip install``, no virtualenv, no system Python dependency beyond libc.

Layout inside the package:

    /opt/aiventbus/
        aiventbus-launcher          ← PyInstaller binary
        _internal/...               ← bundled runtime
    /usr/bin/
        aiventbus -> /opt/aiventbus/aiventbus-launcher
        aibus     -> /opt/aiventbus/aiventbus-launcher
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aiventbus import __version__
from aiventbus.packaging.pyinstaller_build import (
    BUNDLE_NAME,
    LAUNCHER_NAME,
    BundleResult,
    build_bundle,
)

APP_PACKAGE = "aiventbus-daemon"
INSTALL_ROOT = Path("/opt/aiventbus")


@dataclass(frozen=True)
class DebBuildResult:
    deb_path: Path
    staging_dir: Path


def _detect_architecture() -> str:
    dpkg = shutil.which("dpkg")
    if dpkg:
        result = subprocess.run(
            [dpkg, "--print-architecture"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    machine = platform.machine().lower()
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armhf",
    }.get(machine, "all")


def _render_control(
    *,
    package_name: str,
    version: str,
    maintainer: str,
    architecture: str,
    installed_size_kb: int,
) -> str:
    return (
        f"Package: {package_name}\n"
        f"Version: {version}\n"
        "Section: utils\n"
        "Priority: optional\n"
        f"Architecture: {architecture}\n"
        f"Installed-Size: {installed_size_kb}\n"
        "Depends: libc6\n"
        "Recommends: xclip | wl-clipboard, libnotify-bin, xdg-utils\n"
        "Suggests: ollama\n"
        f"Maintainer: {maintainer}\n"
        "Homepage: https://github.com/kosminus/ai-event-bus\n"
        "Description: Local-first AI control plane daemon\n"
        " Event-driven local daemon for orchestrating Ollama-backed agents,\n"
        " event routing, approvals, and desktop-aware automation.\n"
        " .\n"
        " This package bundles a self-contained Python runtime — no system\n"
        " Python or pip install is required.\n"
    )


def _render_postinst() -> str:
    # The bundle is fully self-contained. We only emit a friendly hint.
    return (
        "#!/bin/sh\n"
        "set -e\n"
        "cat <<'EOF'\n"
        "Installed aiventbus-daemon.\n"
        "\n"
        "Next step for each user who should run the daemon:\n"
        "  aibus install\n"
        "\n"
        "That creates and enables the per-user systemd unit using the bundled runtime.\n"
        "EOF\n"
    )


def _render_postrm() -> str:
    # dpkg already removes files it installed; we only handle purge cleanup of
    # anything the app wrote underneath /opt/aiventbus at runtime (none today,
    # but future-proof the hook).
    return (
        "#!/bin/sh\n"
        "set -e\n"
        'if [ "${1:-}" = "purge" ]; then\n'
        f"  rm -rf {INSTALL_ROOT}\n"
        "fi\n"
    )


def _render_readme_debian() -> str:
    return (
        "aiventbus-daemon Debian package\n"
        "===============================\n\n"
        "This package installs a self-contained PyInstaller bundle under\n"
        "/opt/aiventbus. The symlinks /usr/bin/aibus and /usr/bin/aiventbus\n"
        "both point at /opt/aiventbus/aiventbus-launcher — the launcher\n"
        "dispatches on argv[0] to run either the daemon (aiventbus) or the\n"
        "user-facing CLI (aibus).\n\n"
        "After installing the package, each user who wants the daemon running\n"
        "in their session should run:\n\n"
        "  aibus install\n\n"
        "That command creates a user-scoped systemd unit at\n"
        "~/.config/systemd/user/aiventbus.service and enables it.\n"
    )


def _render_copyright() -> str:
    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: aiventbus\n"
        "Source: https://github.com/kosminus/ai-event-bus\n\n"
        "Files: *\n"
        "Copyright: aiventbus contributors\n"
        "License: See the project repository for license details.\n"
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _copy_bundle(bundle_src: Path, install_root_in_staging: Path) -> int:
    """Copy the PyInstaller onedir output into the staging tree.

    Returns the installed size in KB (for the control file).
    """
    shutil.copytree(bundle_src, install_root_in_staging, symlinks=True)

    total_bytes = 0
    for dirpath, _dirnames, filenames in os.walk(install_root_in_staging):
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                total_bytes += fp.stat().st_size
            except OSError:
                pass
    # Debian Installed-Size is in kibibytes, rounded up.
    return max(1, (total_bytes + 1023) // 1024)


def build_deb(
    *,
    bundle: BundleResult | None = None,
    output_dir: str | Path = "dist",
    maintainer: str = "aiventbus maintainers <maintainers@aiventbus.local>",
    package_name: str = APP_PACKAGE,
    revision: str = "1",
    architecture: str | None = None,
    keep_staging: bool = False,
    build_if_missing: bool = True,
) -> DebBuildResult:
    """Package the PyInstaller bundle as a Debian package.

    If ``bundle`` is not provided, this builds one via :func:`build_bundle`
    (unless ``build_if_missing`` is False, in which case the caller must have
    built it already at ``dist/aiventbus-bundle``).
    """

    dpkg_deb = shutil.which("dpkg-deb")
    if not dpkg_deb:
        raise RuntimeError("dpkg-deb is required to build a Debian package")

    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    if bundle is None:
        default_bundle = output_path / BUNDLE_NAME
        if default_bundle.exists():
            bundle = BundleResult(
                bundle_dir=default_bundle,
                launcher_path=default_bundle / LAUNCHER_NAME,
            )
        elif build_if_missing:
            bundle = build_bundle(output_dir=output_path)
        else:
            raise FileNotFoundError(
                f"PyInstaller bundle not found at {default_bundle}. "
                "Run `aibus package-binary` first or pass build_if_missing=True."
            )

    if not bundle.launcher_path.exists():
        raise FileNotFoundError(
            f"Launcher missing at {bundle.launcher_path}. Rebuild the bundle."
        )

    architecture = architecture or _detect_architecture()
    deb_version = f"{__version__}-{revision}"

    tempdir = Path(tempfile.mkdtemp(prefix="aiventbus-deb-"))
    staging_dir = tempdir / f"{package_name}_{deb_version}_{architecture}"

    install_root_in_staging = staging_dir / INSTALL_ROOT.relative_to("/")
    install_root_in_staging.parent.mkdir(parents=True, exist_ok=True)
    installed_size_kb = _copy_bundle(bundle.bundle_dir, install_root_in_staging)

    usr_bin = staging_dir / "usr" / "bin"
    usr_bin.mkdir(parents=True, exist_ok=True)
    launcher_target = INSTALL_ROOT / LAUNCHER_NAME
    for symlink_name in ("aiventbus", "aibus"):
        link_path = usr_bin / symlink_name
        os.symlink(launcher_target, link_path)

    doc_dir = staging_dir / "usr" / "share" / "doc" / package_name
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "README.Debian").write_text(_render_readme_debian(), encoding="utf-8")
    (doc_dir / "copyright").write_text(_render_copyright(), encoding="utf-8")

    debian_dir = staging_dir / "DEBIAN"
    debian_dir.mkdir(parents=True, exist_ok=True)
    (debian_dir / "control").write_text(
        _render_control(
            package_name=package_name,
            version=deb_version,
            maintainer=maintainer,
            architecture=architecture,
            installed_size_kb=installed_size_kb,
        ),
        encoding="utf-8",
    )
    _write_executable(debian_dir / "postinst", _render_postinst())
    _write_executable(debian_dir / "postrm", _render_postrm())

    deb_path = output_path / f"{package_name}_{deb_version}_{architecture}.deb"
    try:
        subprocess.run(
            [dpkg_deb, "--build", "--root-owner-group", str(staging_dir), str(deb_path)],
            check=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            [dpkg_deb, "--build", str(staging_dir), str(deb_path)],
            check=True,
        )

    if not keep_staging:
        shutil.rmtree(tempdir, ignore_errors=True)
        return DebBuildResult(deb_path=deb_path, staging_dir=Path())

    return DebBuildResult(deb_path=deb_path, staging_dir=staging_dir)
