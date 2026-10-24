"""Windows-owned configuration and upgrade contracts, using temporary files only."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "windows"))
from config_helpers import enable_codex_hooks, merge_kimi_hooks, merge_kimi_mcp, set_codex_tool_timeout
from host import WindowsHost
from install import hook_command


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

    def test_kimi_mcp_merge_preserves_fields_servers_env_and_larger_timeout(self):
        original = json.dumps({"other": {"keep": True}, "mcpServers": {
            "other": {"command": "keep"}, "everos": {"description": "keep", "command": "old",
            "env": {"OTHER": "keep"}, "toolTimeoutMs": 300000}}})
        merged = merge_kimi_mcp(original, "C:/Python/python.exe", "D:/runtime/plugin/everos/mcp_server.py",
                                "D:/data", "C:/Users/fixture/.kimi-code", 250000)
        data = json.loads(merged)
        self.assertEqual(data["other"], {"keep": True})
        self.assertEqual(data["mcpServers"]["other"], {"command": "keep"})
        everos = data["mcpServers"]["everos"]
        self.assertEqual(everos["description"], "keep")
        self.assertEqual(everos["command"], "C:/Python/python.exe")
        self.assertEqual(everos["args"], ["-X", "utf8", "D:/runtime/plugin/everos/mcp_server.py"])
        self.assertEqual(everos["env"], {"OTHER": "keep", "EVEROS_HOME": "D:/data", "PYTHONUTF8": "1",
                                        "KIMI_CODE_HOME": "C:/Users/fixture/.kimi-code"})
        self.assertEqual(everos["toolTimeoutMs"], 300000)
        self.assertEqual(merge_kimi_mcp(merged, "C:/Python/python.exe", "D:/runtime/plugin/everos/mcp_server.py",
                                       "D:/data", "C:/Users/fixture/.kimi-code", 250000), merged)
        raised = json.loads(merge_kimi_mcp(merged, "C:/Python/python.exe", "D:/runtime/plugin/everos/mcp_server.py",
                                          "D:/data", "C:/Users/fixture/.kimi-code", 350000))
        self.assertEqual(raised["mcpServers"]["everos"]["toolTimeoutMs"], 350000)

    def test_kimi_mcp_rejects_invalid_existing_timeout(self):
        for value in (0, -1, "invalid", True, None, float("inf"), float("nan")):
            with self.subTest(value=value):
                original = json.dumps({"mcpServers": {"everos": {"toolTimeoutMs": value}}})
                with self.assertRaisesRegex(ValueError, "toolTimeoutMs.*positive finite"):
                    merge_kimi_mcp(original, "python", "server", "home", "kimi", 190000)

    def test_kimi_hooks_replace_only_owned_encoded_entries_and_are_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="everos-kimi-空间 ") as temporary:
            root = Path(temporary)
            old = hook_command(sys.executable, root / "old/everos/hook.py", root / "old-data",
                               host="kimi", kimi_home=root / "old-kimi")
            foreign = hook_command(sys.executable, root / "agent-memory/hook.py", root / "memory-data")
            command = hook_command(sys.executable, root / "runtime/everos/hook.py", root / "data",
                                   host="kimi", kimi_home=root / "kimi home")
            original = ('model = "keep"\nnote = """\n[[hooks]]\ncommand = "not a table"\n"""\n\n'
                        '[[hooks]]\nevent = "Stop"\ncommand = "python3 /old/everos/hook.py" # legacy\ntimeout = 1\n\n'
                        '[[hooks]]\nevent = "Stop"\ncommand = ' + json.dumps(old) + ' # encoded legacy\ntimeout = 1\n\n'
                        '[[hooks]]\nevent = "Stop"\ncommand = ' + json.dumps(foreign) + '\ntimeout = 7\n\n'
                        '[[hooks]]\nevent = "Stop"\ncommand = "echo python3 /tmp/everos/hook.py"\ntimeout = 8\n')
            merged = merge_kimi_hooks(original, command, 250)
            self.assertIn('model = "keep"', merged)
            self.assertIn('note = """\n[[hooks]]\ncommand = "not a table"\n"""', merged)
            self.assertIn(json.dumps(foreign), merged)
            self.assertIn('command = "echo python3 /tmp/everos/hook.py"', merged)
            self.assertNotIn("/old/everos", merged)
            self.assertNotIn(json.dumps(old), merged)
            self.assertEqual(merged.count("[[hooks]]"), 7)
            self.assertIn('event = "UserPromptSubmit"\ncommand = ' + json.dumps(command) + '\ntimeout = 250', merged)
            for event, timeout in (("Stop", 10), ("PreCompact", 10), ("SessionEnd", 5)):
                self.assertIn('event = "{}"\ncommand = {}\ntimeout = {}'.format(event, json.dumps(command), timeout), merged)
            decoded = base64.b64decode(command.split()[-1]).decode("utf-16le")
            for name in ("EVEROS_HOST", "KIMI_CODE_HOME", "EVEROS_HOME", "PYTHONUTF8"):
                self.assertIn("$env:" + name + "=", decoded)
            self.assertIn("$env:EVEROS_HOST='kimi'", decoded)
            self.assertEqual(merge_kimi_hooks(merged, command, 250), merged)

    def test_rules_sync_codex_and_kimi_preserves_other_managed_blocks_and_override(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                "EVEROS_INSTALL_HOME": temporary, "EVEROS_HOME": str(Path(temporary) / ".everos"),
                "CODEX_HOME": str(Path(temporary) / ".codex"),
                "KIMI_CODE_HOME": str(Path(temporary) / ".kimi-code")}):
            root = Path(temporary)
            codex = root / ".codex"
            kimi = root / ".kimi-code"
            codex.mkdir(); kimi.mkdir()
            (codex / "AGENTS.md").write_text("ordinary entry\n", encoding="utf-8")
            override = codex / "AGENTS.override.md"
            override.write_text("override instructions\n", encoding="utf-8")
            native = kimi / "AGENTS.md"
            native.write_text("<!-- agent-memory:start -->\nkeep\n<!-- agent-memory:end -->\n", encoding="utf-8")
            host = WindowsHost(ROOT)
            host.sync_rules()
            self.assertEqual((codex / "AGENTS.md").read_text(encoding="utf-8"), "ordinary entry\n")
            self.assertTrue(override.read_text(encoding="utf-8").startswith("override instructions\n"))
            text = native.read_text(encoding="utf-8")
            self.assertIn("<!-- agent-memory:start -->\nkeep\n<!-- agent-memory:end -->", text)
            self.assertEqual(text.count("<!-- everos-bridge:begin -->"), 1)
            report = json.loads((host.everos / "state/rules/targets.json").read_text(encoding="utf-8"))
            current = json.loads((host.everos / "state/rules/current.json").read_text(encoding="utf-8"))
            self.assertEqual(report["scope"], ["codex", "kimi"])
            self.assertEqual([target["host"] for target in report["targets"]], ["codex", "kimi"])
            self.assertTrue(all(target["status"] == "file_verified" for target in report["targets"]))
            self.assertEqual(report["revision"], current["revision"])
            stamps = {path: path.stat().st_mtime_ns for path in (override, native)}
            host.sync_rules()
            self.assertEqual(stamps, {path: path.stat().st_mtime_ns for path in (override, native)})
            WindowsHost(ROOT, only="kimi").sync_rules()
            limited = json.loads((host.everos / "state/rules/targets.json").read_text(encoding="utf-8"))
            self.assertEqual(limited["scope"], ["codex", "kimi"])
            self.assertEqual({target["host"] for target in limited["targets"]}, {"codex", "kimi"})

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
