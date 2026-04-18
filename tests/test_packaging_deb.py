from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

from aiventbus._launcher import main as launcher_main
from aiventbus.packaging.deb import (
    INSTALL_ROOT,
    _render_control,
    _render_copyright,
    _render_postinst,
    _render_postrm,
    _render_readme_debian,
    build_deb,
)
from aiventbus.packaging.pyinstaller_build import BUNDLE_NAME, LAUNCHER_NAME, BundleResult


class ControlFileTests(unittest.TestCase):
    def test_control_is_self_contained(self) -> None:
        control = _render_control(
            package_name="aiventbus-daemon",
            version="0.1.0-1",
            maintainer="Maintainer <dev@example.com>",
            architecture="amd64",
            installed_size_kb=12345,
        )

        self.assertIn("Package: aiventbus-daemon", control)
        self.assertIn("Version: 0.1.0-1", control)
        self.assertIn("Architecture: amd64", control)
        self.assertIn("Installed-Size: 12345", control)
        # Self-contained bundle → only libc6 is required at the system level.
        self.assertIn("Depends: libc6", control)
        self.assertNotIn("python3", control)
        self.assertNotIn("python3-venv", control)

    def test_postinst_does_not_run_pip(self) -> None:
        postinst = _render_postinst()
        self.assertNotIn("pip", postinst)
        self.assertNotIn("venv", postinst)
        self.assertIn("aibus install", postinst)

    def test_postrm_only_cleans_on_purge(self) -> None:
        postrm = _render_postrm()
        self.assertIn('if [ "${1:-}" = "purge" ]; then', postrm)
        self.assertIn(f"rm -rf {INSTALL_ROOT}", postrm)

    def test_readme_and_copyright_present(self) -> None:
        self.assertIn("/opt/aiventbus", _render_readme_debian())
        self.assertIn("aibus install", _render_readme_debian())
        self.assertIn("Format:", _render_copyright())


class LauncherDispatchTests(unittest.TestCase):
    """The launcher chooses daemon vs CLI based on argv[0]."""

    def test_daemon_dispatch(self) -> None:
        import sys
        import unittest.mock as mock

        fake_daemon = mock.MagicMock()
        fake_cli = mock.MagicMock()
        with mock.patch.dict(sys.modules, {
            "aiventbus.main": mock.MagicMock(cli=fake_daemon),
            "aiventbus.cli": mock.MagicMock(main=fake_cli),
        }), mock.patch.object(sys, "argv", ["/usr/bin/aiventbus"]):
            launcher_main()
        fake_daemon.assert_called_once()
        fake_cli.assert_not_called()

    def test_cli_dispatch(self) -> None:
        import sys
        import unittest.mock as mock

        fake_daemon = mock.MagicMock()
        fake_cli = mock.MagicMock()
        with mock.patch.dict(sys.modules, {
            "aiventbus.main": mock.MagicMock(cli=fake_daemon),
            "aiventbus.cli": mock.MagicMock(main=fake_cli),
        }), mock.patch.object(sys, "argv", ["/usr/bin/aibus"]):
            launcher_main()
        fake_cli.assert_called_once()
        fake_daemon.assert_not_called()


@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb not available")
class EndToEndDebTests(unittest.TestCase):
    """Build a .deb against a fake bundle — exercises staging + symlinks."""

    def test_build_deb_from_fake_bundle(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bundle_dir = tmp_path / BUNDLE_NAME
            fake_bundle_dir.mkdir()
            launcher = fake_bundle_dir / LAUNCHER_NAME
            launcher.write_text("#!/bin/sh\necho fake\n")
            launcher.chmod(0o755)
            (fake_bundle_dir / "_internal").mkdir()
            (fake_bundle_dir / "_internal" / "placeholder").write_text("x")

            result = build_deb(
                bundle=BundleResult(bundle_dir=fake_bundle_dir, launcher_path=launcher),
                output_dir=tmp_path,
                architecture="amd64",
                keep_staging=True,
            )

            self.assertTrue(result.deb_path.exists())
            self.assertTrue(result.deb_path.name.endswith("_amd64.deb"))

            usr_bin = result.staging_dir / "usr" / "bin"
            aibus_link = usr_bin / "aibus"
            aiventbus_link = usr_bin / "aiventbus"
            self.assertTrue(aibus_link.is_symlink())
            self.assertTrue(aiventbus_link.is_symlink())
            self.assertEqual(
                os.readlink(aibus_link),
                str(INSTALL_ROOT / LAUNCHER_NAME),
            )


if __name__ == "__main__":
    unittest.main()
