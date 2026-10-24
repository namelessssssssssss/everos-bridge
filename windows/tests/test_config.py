"""Windows-owned configuration and upgrade contracts, using temporary files only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "windows"))
from config_helpers import enable_codex_hooks, set_codex_tool_timeout
from host import WindowsHost


class WindowsConfigurationTests(unittest.TestCase):
    def test_timeout_and_hooks_preserve_unrelated_tables_multiline_strings_and_larger_timeout(self):
        original = 'model = "keep"\nnote = """\n[features]\nhooks = false\n"""\n[features]\nhooks = false\nother = true\n[mcp_servers.other]\ncommand = "keep"\n[mcp_servers.everos]\ntool_timeout_sec = 300\n'
        updated = set_codex_tool_timeout(enable_codex_hooks(original), 190)
        self.assertIn('note = """\n[features]\nhooks = false\n"""', updated)
        self.assertIn('[features]\nhooks = true\nother = true', updated)
        self.assertIn('[mcp_servers.other]\ncommand = "keep"', updated)
        self.assertIn('tool_timeout_sec = 300', updated)
        self.assertEqual(set_codex_tool_timeout(updated, 190), updated)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            enable_codex_hooks('[features]\nhooks=false\n[features]\nhooks=true\n')

    def test_rule_update_preserves_user_text_backs_up_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                "EVEROS_INSTALL_HOME": temporary, "EVEROS_HOME": str(Path(temporary) / ".everos"),
                "CODEX_HOME": str(Path(temporary) / ".codex")}):
            host = WindowsHost(ROOT)
            path = Path(temporary) / ".codex/AGENTS.md"
            path.parent.mkdir()
            path.write_text("user instructions\n", encoding="utf-8")
            host.sync_rules()
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("user instructions\n"))
            self.assertEqual(text.count('<!-- everos-bridge:begin -->'), 1)
            self.assertIn("install.ps1", text)
            stamp = path.stat().st_mtime_ns
            host.sync_rules()
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            backups = list(path.parent.glob('AGENTS.md.bak-everos-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "user instructions\n")
            report = json.loads((host.everos / "state/rules/targets.json").read_text(encoding="utf-8"))
            self.assertEqual(report["scope"], ["codex"])
            self.assertEqual(report["targets"][0]["status"], "file_verified")

    def test_failed_upgrade_restores_old_cache_without_replacing_new_cache(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                "EVEROS_INSTALL_HOME": temporary, "CODEX_HOME": str(Path(temporary) / ".codex")}):
            host = WindowsHost(ROOT)
            root = Path(temporary) / ".codex/plugins/cache/everos-bridge/everos"
            old = root / "old/everos/hook.py"
            old.parent.mkdir(parents=True)
            old.write_text("old implementation", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "fixture upgrade failed"):
                with host.preserve_codex_cache():
                    old.unlink()
                    old.parent.rmdir()
                    old.parent.parent.rmdir()
                    new = root / "new/everos/hook.py"
                    new.parent.mkdir(parents=True)
                    new.write_text("new implementation", encoding="utf-8")
                    raise RuntimeError("fixture upgrade failed")
            self.assertEqual(old.read_text(encoding="utf-8"), "old implementation")
            self.assertEqual(new.read_text(encoding="utf-8"), "new implementation")
