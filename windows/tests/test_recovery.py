"""Logon drift recovery against isolated homes; no real CLI, API or task writes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "windows"))
from install import WindowsInstaller
import recover


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        env = patch.dict(os.environ, {"EVEROS_INSTALL_HOME": str(self.home),
                "EVEROS_HOME": str(self.home / ".everos"), "CODEX_HOME": str(self.home / ".codex")})
        env.start()
        self.addCleanup(env.stop)
        self.installer = WindowsInstaller(ROOT)
        self.runtime = self.installer.snapshot()
        WindowsInstaller(self.runtime).install_cli()
        self.native = self.home / ".codex/AGENTS.md"
        self.native.parent.mkdir(exist_ok=True)
        self.native.write_text("user rules\n", encoding="utf-8")
        self.installer.sync_rules()
        self.installer.protocol_link()
        q = lambda value: json.dumps(str(value))
        self.good_config = (
            '[features]\nhooks = true\n[plugins."everos@everos-bridge"]\nenabled = true\n'
            '[marketplaces.everos-bridge]\nsource = ' + q(self.runtime) + '\n'
            '[mcp_servers.everos]\ncommand = ' + q(self.installer.python) + '\n'
            'args = ["-X", "utf8", ' + q(self.runtime / "plugin/everos/mcp_server.py") + ']\n'
            'tool_timeout_sec = 999\n[mcp_servers.everos.env]\nEVEROS_HOME = ' + q(self.installer.everos) + '\n'
            '[hooks.state.unrelated]\ntrusted_hash = "keep"\n')
        self.config = self.home / ".codex/config.toml"
        self.config.write_text(self.good_config, encoding="utf-8")

    def test_healthy_check_does_not_reinstall_or_rewrite_files(self):
        before = self.config.stat().st_mtime_ns, self.native.stat().st_mtime_ns
        with patch.object(self.installer, "install") as install:
            self.assertEqual(recover.recover(self.installer)["repair"], "none")
            install.assert_not_called()
        self.assertEqual(before, (self.config.stat().st_mtime_ns, self.native.stat().st_mtime_ns))

    def test_extended_windows_marketplace_prefix_is_the_same_directory(self):
        extended = "\\\\?\\" + str(self.runtime)
        config = self.good_config.replace(json.dumps(str(self.runtime)), json.dumps(extended))
        self.config.write_text(config, encoding="utf-8")
        self.assertEqual(recover.recover(self.installer)["issues"], [])

    def test_rule_drift_is_automatically_repaired_without_touching_config(self):
        self.native.write_text(self.native.read_text(encoding="utf-8").replace("## EverOS", "## Obsolete"), encoding="utf-8")
        original_config = self.config.read_bytes()
        check = recover.recover(self.installer, check_only=True)
        self.assertFalse(check["ok"])
        self.assertIn("Obsolete", self.native.read_text(encoding="utf-8"))
        result = recover.recover(self.installer)
        self.assertEqual(result["repair"], "rules")
        self.assertTrue(self.native.read_text(encoding="utf-8").startswith("user rules\n"))
        self.assertEqual(self.config.read_bytes(), original_config)
        self.assertEqual(recover.recover(self.installer)["repair"], "none")

    def test_missing_mcp_selects_full_installer_and_checks_result(self):
        self.config.write_text(self.good_config.replace("mcp_servers.everos", "mcp_servers.absent"), encoding="utf-8")
        def repair():
            self.config.write_text(self.good_config, encoding="utf-8")
        with patch.object(self.installer, "install", side_effect=repair) as install:
            self.assertEqual(recover.recover(self.installer)["repair"], "install")
            install.assert_called_once()
        with patch.object(self.installer, "install"):
            self.config.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "readback failed"):
                recover.recover(self.installer)

    def test_damaged_managed_block_is_preserved_and_reported(self):
        self.native.write_text("user rules\n<!-- everos-bridge:begin -->\n", encoding="utf-8")
        before = self.native.read_bytes()
        with patch.object(self.installer, "install") as install:
            with self.assertRaisesRegex(ValueError, "boundaries"):
                recover.recover(self.installer)
            install.assert_not_called()
        self.assertEqual(self.native.read_bytes(), before)
