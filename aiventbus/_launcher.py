"""Single entry point for the PyInstaller-built binary.

The built executable is installed once under ``/opt/aiventbus`` and symlinked
into ``/usr/bin`` as both ``aiventbus`` and ``aibus``. PyInstaller sets
``sys.argv[0]`` to the symlink name, so we dispatch on that.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    invoked_as = os.path.basename(sys.argv[0] or "").lower()
    # Normalize common suffixes (".exe" on weird cross-builds, trailing version).
    if invoked_as.endswith(".exe"):
        invoked_as = invoked_as[:-4]

    if invoked_as.startswith("aiventbus"):
        from aiventbus.main import cli as daemon_cli

        daemon_cli()
        return

    # Default to the user-facing CLI. Covers "aibus" and anything unexpected.
    from aiventbus.cli import main as aibus_main

    aibus_main()


if __name__ == "__main__":
    main()
