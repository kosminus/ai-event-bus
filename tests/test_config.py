from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from aiventbus.config import load_config


class ConfigLoadTests(unittest.TestCase):
    def test_load_config_prefers_cli_over_env_and_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            cli_cfg = temp_path / "cli.yaml"
            env_cfg = temp_path / "env.yaml"
            cli_db = temp_path / "cli.db"

            cli_cfg.write_text("server:\n  port: 9100\n", encoding="utf-8")
            env_cfg.write_text(
                textwrap.dedent(
                    """
                    server:
                      port: 9200
                    database:
                      path: env.db
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "AIVENTBUS_CONFIG": str(env_cfg),
                    "AIVENTBUS_DB": str(temp_path / "env.db"),
                },
                clear=False,
            ):
                cfg = load_config(config_path=cli_cfg, db_path=cli_db, dev=False)

            self.assertEqual(cfg.server.port, 9100)
            self.assertEqual(cfg.database.path, str(cli_db))
            self.assertEqual(cfg.sources.config_path_source, "cli")
            self.assertEqual(cfg.sources.db_path_source, "cli")

    def test_load_config_uses_dev_cwd_files_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            (temp_path / "config.yaml").write_text(
                "server:\n  port: 9300\n",
                encoding="utf-8",
            )
            dev_db = temp_path / "aiventbus.db"
            dev_db.write_text("", encoding="utf-8")

            old_cwd = Path.cwd()
            try:
                os.chdir(temp_path)
                with patch.dict(os.environ, {}, clear=False):
                    cfg = load_config(dev=True)
            finally:
                os.chdir(old_cwd)

            self.assertEqual(cfg.server.port, 9300)
            self.assertEqual(Path(cfg.database.path).resolve(), dev_db.resolve())
            self.assertEqual(cfg.sources.config_path_source, "dev_cwd")
            self.assertEqual(cfg.sources.db_path_source, "dev_cwd")
            self.assertTrue(cfg.sources.dev_mode)
