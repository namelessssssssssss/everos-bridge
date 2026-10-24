"""Logon drift recovery against isolated homes; no real CLI, API or task writes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "windows"))
from install import WindowsInstaller
import recover
import provenance


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
        head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        git_state = patch.object(provenance, "git_state", return_value=(head, False))
        git_state.start()
        self.addCleanup(git_state.stop)
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
        self.installer.record_source(self.runtime)

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

    def test_stale_task_cannot_repair_either_wiring_or_native_rules(self):
        newer = self.home / "newer-checkout"
        newer.mkdir()
        receipt = provenance.receipt_path(self.installer)
        binding = json.loads(receipt.read_text(encoding="utf-8"))
        binding["source_root"] = str(newer)
        receipt.write_text(json.dumps(binding), encoding="utf-8")
        for mode in ("wiring", "rules"):
            with self.subTest(mode=mode), patch.object(self.installer, "install") as install, \
                    patch.object(self.installer, "sync_rules") as sync:
                if mode == "wiring":
                    self.config.write_text("", encoding="utf-8")
                else:
                    self.config.write_text(self.good_config, encoding="utf-8")
                    self.native.write_text(self.native.read_text(encoding="utf-8").replace("## EverOS", "## Changed"), encoding="utf-8")
                before = self.config.read_bytes(), self.native.read_bytes(), receipt.read_bytes()
                with self.assertRaisesRegex(ValueError, "Stale recovery task"):
                    recover.recover(self.installer)
                install.assert_not_called()
                sync.assert_not_called()
                self.assertEqual(before, (self.config.read_bytes(), self.native.read_bytes(), receipt.read_bytes()))

    def test_missing_or_dirty_source_receipt_requires_explicit_confirmation(self):
        receipt = provenance.receipt_path(self.installer)
        receipt.unlink()
        with self.assertRaisesRegex(ValueError, "receipt is missing"):
            recover.recover(self.installer)
        self.installer.record_source(self.runtime)
        with patch.object(provenance, "git_state", return_value=("a" * 40, True)):
            with self.assertRaisesRegex(ValueError, "checkout must be clean"):
                recover.recover(self.installer)

    def test_unknown_existing_runtime_and_newer_rule_marker_are_preserved(self):
        new_runtime = self.home / "new-runtime"
        mcp = new_runtime / "plugin/everos/mcp_server.py"
        mcp.parent.mkdir(parents=True)
        mcp.write_text("newer", encoding="utf-8")
        self.config.write_text(self.good_config.replace(str(self.runtime).replace('\\', '\\\\'), str(new_runtime).replace('\\', '\\\\')), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unconfirmed installed runtime"):
            recover.recover(self.installer)
        self.config.write_text(self.good_config, encoding="utf-8")
        text = self.native.read_text(encoding="utf-8")
        receipt = json.loads(provenance.receipt_path(self.installer).read_text(encoding="utf-8"))
        self.native.write_text(text.replace(receipt["rules_revision"], "f" * 64), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unconfirmed installed rules"):
            recover.recover(self.installer)

    def test_missing_and_tampered_snapshot_files_are_never_overwritten(self):
        for name in ("plugin/everos/mcp_server.py", "plugin/everos/hook.py",
                     "plugin/hooks/hooks.json", "plugin/.codex-plugin/plugin.json", "plugin/everos/cli.py"):
            path = self.runtime / name
            original = path.read_bytes()
            for missing in (True, False):
                with self.subTest(path=name, missing=missing):
                    if missing:
                        path.unlink()
                    else:
                        path.write_bytes(b"damaged")
                    before = self.config.read_bytes(), self.native.read_bytes()
                    with patch.object(self.installer, "install") as install:
                        with self.assertRaisesRegex(ValueError, "Existing runtime snapshot differs"):
                            recover.recover(self.installer)
                        install.assert_not_called()
                    self.assertEqual(before, (self.config.read_bytes(), self.native.read_bytes()))
                    self.assertEqual(None if missing else b"damaged", path.read_bytes() if path.exists() else None)
                    path.write_bytes(original)

    def test_rollback_in_same_checkout_is_rejected_but_descendant_is_allowed(self):
        repo = self.home / "git-source"
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
        git("init")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (repo / "file").write_text("A", encoding="utf-8")
        git("add", "file")
        git("commit", "-m", "A")
        older = git("rev-parse", "HEAD")
        (repo / "file").write_text("B", encoding="utf-8")
        git("commit", "-am", "B")
        newer = git("rev-parse", "HEAD")
        receipt = provenance.receipt_path(self.installer)
        binding = json.loads(receipt.read_text(encoding="utf-8"))
        binding.update(source_root=str(repo), source_revision=older)
        receipt.write_text(json.dumps(binding), encoding="utf-8")
        self.installer.repo = repo
        with patch.object(provenance, "git_state", return_value=(newer, False)):
            provenance.verify(self.installer)
        binding["source_revision"] = newer
        receipt.write_text(json.dumps(binding), encoding="utf-8")
        git("checkout", older)
        with patch.object(provenance, "git_state", return_value=(older, False)):
            with self.assertRaisesRegex(ValueError, "older than or diverges"):
                provenance.verify(self.installer)
