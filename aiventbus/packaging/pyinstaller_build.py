"""Run PyInstaller to build a self-contained daemon + CLI bundle.

The spec at ``packaging/aiventbus.spec`` produces ``dist/aiventbus-bundle/``
containing:

- ``aiventbus-launcher`` — the dispatcher executable
- ``_internal/`` — bundled Python runtime + dependencies + app data

This module wraps the subprocess invocation so it's callable from the ``aibus``
CLI and from tests.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BUNDLE_NAME = "aiventbus-bundle"
LAUNCHER_NAME = "aiventbus-launcher"


@dataclass(frozen=True)
class BundleResult:
    bundle_dir: Path
    launcher_path: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def spec_path() -> Path:
    return _repo_root() / "packaging" / "aiventbus.spec"


def build_bundle(
    *,
    output_dir: str | Path = "dist",
    work_dir: str | Path = "build",
    clean: bool = True,
) -> BundleResult:
    """Invoke PyInstaller and return the produced bundle path.

    Requires PyInstaller to be importable in the current interpreter
    (``pip install pyinstaller`` — kept out of the runtime deps since it's only
    needed by maintainers).
    """

    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "PyInstaller is not installed. Install it with `pip install pyinstaller` "
            "before building the binary bundle."
        ) from exc

    spec = spec_path()
    if not spec.exists():
        raise FileNotFoundError(f"PyInstaller spec not found at {spec}")

    output_path = Path(output_dir).expanduser().resolve()
    work_path = Path(work_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    work_path.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(spec),
        "--noconfirm",
        "--distpath",
        str(output_path),
        "--workpath",
        str(work_path),
    ]
    if clean:
        cmd.append("--clean")

    subprocess.run(cmd, check=True, cwd=_repo_root())

    bundle_dir = output_path / BUNDLE_NAME
    launcher = bundle_dir / LAUNCHER_NAME
    if not launcher.exists():
        raise RuntimeError(
            f"PyInstaller finished but {launcher} is missing. Check the build logs."
        )

    return BundleResult(bundle_dir=bundle_dir, launcher_path=launcher)


def have_pyinstaller() -> bool:
    return shutil.which("pyinstaller") is not None or _module_available("PyInstaller")


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False
