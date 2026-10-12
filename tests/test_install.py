"""Offline installer regression tests: isolated paths and fake host CLIs."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_config", ROOT / "scripts/install_config.py")
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)
try:
    import tomllib
except ImportError:  # The installer itself supports Python 3.9 without dependencies.
    tomllib = None


FAKE_CLI = r'''
import json, os, shutil, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
home = Path(os.environ["EVEROS_INSTALL_HOME"])
with open(os.environ["FAKECLI_LOG"], "a") as output:
    output.write(json.dumps([name] + args) + "\n")
action = " ".join([name] + args)
if name == "codex" and args[:2] == ["plugin", "add"] and os.environ.get("FAKECLI_PRUNE_CODEX_CACHE"):
    cache = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "plugins/cache/everos-bridge/everos"
    if cache.exists():
        shutil.rmtree(cache)
    new = cache / "0.3.2/everos/hook.py"
    new.parent.mkdir(parents=True)
    new.write_text("new hook\n")
if os.environ.get("FAIL_ACTION") and action.startswith(os.environ["FAIL_ACTION"]):
    print("simulated host CLI failure", file=sys.stderr)
    sys.exit(9)
if name == "ps":
    print("/Applications/Claude.app/Contents/MacOS/Claude" if os.environ.get("CLAUDE_RUNNING") else "")
elif args[:2] == ["plugin", "marketplace"]:
    path = home / ("." + name + "-marketplaces.json")
    sources = json.loads(path.read_text()) if path.exists() else {}
    if args[2] == "list":
        if name == "codex":
            print(json.dumps({"marketplaces": [{"name": key, "root": value,
                  "marketplaceSource": {"sourceType": "local", "source": value}}
                  for key, value in sources.items()]}))
        else:
            print(json.dumps([{"name": key, "source": "directory", "path": value}
                              for key, value in sources.items()]))
    elif args[2] == "remove":
        sources.pop(args[3], None)
        path.write_text(json.dumps(sources))
    elif args[2] == "add" and not os.environ.get("FAIL_MIGRATION"):
        sources["everos-bridge"] = args[3]
        path.write_text(json.dumps(sources))
elif args[:2] == ["mcp", "get"]:
    if os.environ.get("FAKE_MCP_PRESENT", "1") != "1":
        print("Error: No MCP server named 'everos' found.", file=sys.stderr)
        sys.exit(1)
elif name == "codex" and args[:2] == ["mcp", "add"] and os.environ.get("FAKECLI_MCP_REPLACE"):
    path = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "config.toml"
    path.write_text('[mcp_servers.everos]\ncommand = "python3"\n')
elif name == "codex" and args[:2] == ["plugin", "add"] and os.environ.get("FAKECLI_MUTATE"):
    path = home / ".codex/config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as output:
        output.write("# CLI wrote this before the file editor\n")
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "host"
        self.home.mkdir()
        self.repo = self.base / "bridge with spaces"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "plugin/everos").mkdir(parents=True)
        shutil.copy2(ROOT / "plugin/everos/core.py", self.repo / "plugin/everos/core.py")
        (self.repo / "plugin/rules").mkdir(parents=True)
        shutil.copy2(ROOT / "install.sh", self.repo / "install.sh")
        shutil.copy2(ROOT / "scripts/install_config.py", self.repo / "scripts/install_config.py")
        (self.repo / "bin").mkdir()
        shutil.copy2(ROOT / "bin/everos", self.repo / "bin/everos")
        (self.repo / "plugin/rules/everos-rules.md").write_text("每条提问检索；每轮对话入库。\n")
        (self.repo / "plugin/skills/everos-memory").mkdir(parents=True)
        (self.repo / "plugin/skills/everos-memory/SKILL.md").write_text("repo skill v1\n")
        (self.repo / "PROTOCOL.md").write_text("# 协议\n")
        (self.repo / "plugin/everos/mcp_server.py").write_text("")
        (self.repo / "plugin/everos/hook.py").write_text(
            "import os, sys\n"
            "with open(os.environ['FAKECLI_LOG'], 'a') as output: output.write('[\"hook-doctor\"]\\n')\n"
            "sys.exit(int(os.environ.get('DOCTOR_EXIT', '0')))\n")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.log = self.base / "cli.jsonl"
        # HOME is untouched. All file paths and host environments use the
        # installer-specific root, and only fixture CLIs are on PATH.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("EVEROS_", "CODEX_", "CLAUDE_", "KIMI_", "TRAE"))}
        self.env.update(EVEROS_INSTALL_HOME=str(self.home), FAKECLI_LOG=str(self.log),
                        EVEROS_USER_ID="test-owner", EVEROS_API_KEY="not-a-real-key",
                        PATH=str(self.bin) + ":/usr/bin:/bin")
        self.cli("ps")

    def cli(self, name):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
        path.chmod(0o755)

    def put(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_install(self, *args, expect=0):
        result = subprocess.run(["/bin/bash", str(self.repo / "install.sh"), *args], env=self.env,
                                text=True, capture_output=True, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_toml(self, text):
        if tomllib:
            return tomllib.loads(text)

    def test_existing_config_requires_credentials_and_does_not_touch_hosts(self):
        self.cli("codex")
        path = self.put(".everos/config.env", "# Empty existing config\n")
        del self.env["EVEROS_API_KEY"]
        result = self.run_install(expect=1)
        self.assertIn("缺少 EVEROS_API_KEY", result.stderr)
        self.assertEqual(path.read_text(), "# Empty existing config\n")
        self.assertEqual(self.calls(), [])

    def test_cli_only_installs_idempotently_without_credentials_or_host_calls(self):
        self.cli("codex")
        del self.env["EVEROS_USER_ID"]
        del self.env["EVEROS_API_KEY"]
        self.env["DOCTOR_EXIT"] = "1"
        self.run_install("--cli-only")
        link = self.home / ".local/bin/everos"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.repo / "bin/everos")
        before = link.lstat().st_mtime_ns
        self.run_install("--cli-only")
        self.assertEqual(link.lstat().st_mtime_ns, before)
        self.assertFalse((self.home / ".everos").exists())
        self.assertFalse((self.home / ".codex").exists())
        self.assertEqual(self.calls(), [])

    def test_cli_only_dry_run_does_not_create_directories(self):
        result = self.run_install("--cli-only", "--dry-run")
        self.assertIn("[dry-run]", result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.calls(), [])

    def test_cli_install_preserves_conflicting_file_and_broken_link(self):
        link = self.put(".local/bin/everos", "another command\n")
        result = self.run_install("--cli-only", expect=1)
        self.assertIn("未覆盖", result.stderr)
        self.assertEqual(link.read_text(), "another command\n")
        link.unlink()
        link.symlink_to("/nonexistent/another-command")
        self.run_install("--cli-only", expect=1)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "/nonexistent/another-command")
        link.unlink()
        link.symlink_to("everos")  # Self-loop must report a conflict without a traceback.
        result = self.run_install("--cli-only", expect=1)
        self.assertIn("未覆盖", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(os.readlink(link), "everos")

    def test_full_install_also_installs_cli(self):
        self.run_install()
        self.assertEqual((self.home / ".local/bin/everos").resolve(), self.repo / "bin/everos")

    def test_config_is_completed_and_permissions_are_0600(self):
        path = self.put(".everos/config.env", "EVEROS_USER_ID=test-owner\nCUSTOM=keep\n")
        path.chmod(0o644)
        self.run_install()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("CUSTOM=keep", path.read_text())
        self.assertIn("EVEROS_API_KEY=not-a-real-key", path.read_text())
        backups = list(path.parent.glob("config.env.bak-everos-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "EVEROS_USER_ID=test-owner\nCUSTOM=keep\n")

    def test_same_timestamp_backups_never_overwrite(self):
        path = self.put("file.json", "first")
        with mock.patch.object(install, "datetime") as timestamp:
            timestamp.now.return_value.strftime.return_value = "20261008000000"
            install.backup(path)
            path.write_text("second")
            install.backup(path)
        backups = list(path.parent.glob("file.json.bak-everos-*"))
        self.assertEqual(len(backups), 2)
        self.assertEqual({path.read_text() for path in backups}, {"first", "second"})

    def test_backup_is_private_while_contents_are_being_copied(self):
        path = self.put("credentials.env", "test-credential")
        path.chmod(0o644)
        original_copy = shutil.copyfileobj

        def check_permissions(source, destination):
            self.assertEqual(os.fstat(destination.fileno()).st_mode & 0o777, 0o600)
            original_copy(source, destination)

        with mock.patch.object(install.shutil, "copyfileobj", side_effect=check_permissions):
            install.backup(path)
        self.assertEqual(next(path.parent.glob("credentials.env.bak-everos-*")).stat().st_mode & 0o777, 0o600)

    def test_codex_features_table_updates_only_its_own_key(self):
        self.cli("codex")
        original = '[other]\nhooks = true\n[features]\nhooks = false\nother = true\n'
        path = self.put(".codex/config.toml", original)
        self.run_install()
        result = self.assert_toml(path.read_text())
        if result:
            self.assertTrue(result["features"]["hooks"])
            self.assertTrue(result["other"]["hooks"])
        self.assertIn("[features]\nhooks = true\nother = true", path.read_text())
        self.assertEqual(path.read_text().count("hooks = true"), 2)
        previous = path.read_text()
        self.run_install()
        self.assertEqual(path.read_text(), previous)

    def test_codex_other_table_does_not_suppress_hooks(self):
        text = install.enable_codex_hooks('[other]\nhooks = true\n')
        self.assertIn("[features]\nhooks = true", text)
        self.assert_toml(text)

    def test_codex_timeout_defaults_to_agentic_budget_with_transport_headroom(self):
        self.cli("codex")
        path = self.put(".codex/config.toml", '[mcp_servers.other]\ntool_timeout_sec = 15\n')
        self.run_install()
        text = path.read_text()
        self.assertIn("[mcp_servers.everos]\ntool_timeout_sec = 190", text)
        self.assertIn("[mcp_servers.other]\ntool_timeout_sec = 15", text)
        self.assert_toml(text)
        self.run_install()
        self.assertEqual(path.read_text(), text)

    def test_codex_timeout_uses_config_environment_priority_and_keeps_larger_values(self):
        self.cli("codex")
        self.put(".everos/config.env", "EVEROS_USER_ID=test-owner\nEVEROS_API_KEY=not-a-real-key\nEVEROS_AGENTIC_TIMEOUT=240.1\n")
        path = self.put(".codex/config.toml", '[mcp_servers.everos]\ntool_timeout_sec = 50\n')
        self.run_install()
        self.assertIn("tool_timeout_sec = 251", path.read_text())
        self.env["EVEROS_AGENTIC_TIMEOUT"] = "300"
        self.run_install()
        self.assertIn("tool_timeout_sec = 310", path.read_text())
        # The real host's remove/add discards this table: restore the previous
        # timeout even when its value is above the current runtime default.
        self.env["FAKECLI_MCP_REPLACE"] = "1"
        path.write_text('[mcp_servers.everos]\ncommand = "old"\ntool_timeout_sec = 600\n')
        self.run_install()
        self.assertIn("tool_timeout_sec = 600", path.read_text())

    def test_codex_timeout_updates_only_active_everos_table(self):
        original = ('description = """\n[mcp_servers.everos]\ntool_timeout_sec = 999\n"""\n'
                    '[mcp_servers.other]\ntool_timeout_sec = 900\n'
                    '[ mcp_servers."everos" ]\n"tool_timeout_sec" = 50\ncommand = "keep"\n')
        text = install.set_codex_tool_timeout(original, 190)
        self.assertIn('description = """\n[mcp_servers.everos]\ntool_timeout_sec = 999\n"""', text)
        self.assertIn('[mcp_servers.other]\ntool_timeout_sec = 900', text)
        self.assertIn('[ mcp_servers."everos" ]\ntool_timeout_sec = 190\ncommand = "keep"', text)
        self.assertEqual(install.set_codex_tool_timeout(text, 180), text)
        self.assert_toml(text)
        self.assertEqual(install.set_codex_tool_timeout('[mcp_servers.everos]', 190),
                         '[mcp_servers.everos]\ntool_timeout_sec = 190\n')

    def test_invalid_agentic_timeout_stops_before_host_calls_or_writes(self):
        self.cli("codex")
        self.cli("claude")
        for value in ("0", "-1", "NaN", "Infinity", "bad"):
            with self.subTest(value=value):
                self.env["EVEROS_AGENTIC_TIMEOUT"] = value
                result = self.run_install(expect=1)
                self.assertIn("EVEROS_AGENTIC_TIMEOUT", result.stderr)
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.home / ".everos").exists())

    def test_unverified_mcp_hosts_do_not_receive_invented_timeout_fields(self):
        gemini = self.put(".gemini/settings.json", '{"mcpServers":{"other":{"timeout":9000}}}')
        cursor = self.put(".cursor/mcp.json", '{}')
        self.run_install()
        gemini_data = json.loads(gemini.read_text())
        self.assertEqual(gemini_data["mcpServers"]["other"], {"timeout": 9000})
        for path in (gemini, cursor):
            self.assertEqual(set(json.loads(path.read_text())["mcpServers"]["everos"]), {"command", "args"})

    def test_codex_dotted_and_duplicate_hook_keys(self):
        self.assertEqual(install.enable_codex_hooks("features.hooks = false\n"), "features.hooks = true\n")
        text = install.enable_codex_hooks("[features]\nhooks = false\nhooks = true\n")
        self.assertEqual(text.count("hooks ="), 1)
        self.assert_toml(text)

    def test_codex_quoted_dotted_keys_update_and_inline_tables_are_rejected(self):
        for original in ('"features"."hooks" = false\n', "'features'.'hooks' = false\n"):
            text = install.enable_codex_hooks(original)
            self.assertEqual(text, "features.hooks = true\n")
            self.assert_toml(text)
        for original in ('"features" = { hooks = false }\n', "'features' = { hooks = false }\n"):
            with self.assertRaisesRegex(ValueError, "Use a \\[features\\] table"):
                install.enable_codex_hooks(original)

    def test_multiline_toml_does_not_create_false_table(self):
        original = 'prompt = """\n[features]\nhooks = false\n"""\n[other]\nhooks = false\n'
        text = install.enable_codex_hooks(original)
        self.assertTrue(text.startswith(original.rstrip()))
        self.assert_toml(text)

    def test_multiline_features_values_and_header_without_newline(self):
        original = '[ features ] # preserve header\nhooks = false\ndescription = """\nhooks = false\n"""\n'
        text = install.enable_codex_hooks(original)
        self.assertIn('description = """\nhooks = false\n"""', text)
        self.assertIn("hooks = true", text)
        self.assert_toml(text)
        self.assertEqual(install.enable_codex_hooks("[features]"), "[features]\nhooks = true\n")

    def test_codex_backup_happens_before_cli_write_even_when_hooks_enabled(self):
        self.cli("codex")
        original = "[features]\nhooks = true\n"
        path = self.put(".codex/config.toml", original)
        self.env["FAKECLI_MUTATE"] = "1"
        self.run_install()
        backups = list(path.parent.glob("config.toml.bak-everos-*"))
        self.assertIn(original, [path.read_text() for path in backups])
        self.assertIn("CLI wrote this", path.read_text())

    def test_codex_creates_missing_config_parent_and_explains_trust(self):
        self.cli("codex")
        result = self.run_install()
        self.assertIn("[features]\nhooks = true", (self.home / ".codex/config.toml").read_text())
        self.assertIn("审阅并信任", result.stdout)
        self.assertFalse(any("trust" in str(call) for call in self.calls()))
        self.assertFalse((self.home / ".codex/AGENTS.md").exists())

    def test_codex_upgrade_preserves_old_cache_and_leaves_new_cache_untouched(self):
        self.cli("codex")
        self.env["FAKECLI_PRUNE_CODEX_CACHE"] = "1"
        for root in (".codex", "alternate-codex"):
            with self.subTest(root=root):
                self.env["CODEX_HOME"] = str(self.home / root)
                base = root + "/plugins/cache/everos-bridge/everos/"
                old = self.put(base + "0.3.1/everos/hook.py", "old hook\n")
                core = self.put(base + "0.3.1/everos/core.py", "old core\n")
                result = self.run_install()
                self.assertEqual(old.read_text(), "old hook\n")
                self.assertEqual(core.read_text(), "old core\n")
                self.assertEqual((self.home / base / "0.3.2/everos/hook.py").read_text(), "new hook\n")
                self.assertIn("已恢复旧版缓存", result.stdout)

    def test_codex_failed_upgrade_also_restores_removed_old_cache(self):
        self.cli("codex")
        old = self.put(".codex/plugins/cache/everos-bridge/everos/0.3.1/everos/hook.py", "old hook\n")
        self.env.update(FAKECLI_PRUNE_CODEX_CACHE="1", FAIL_ACTION="codex plugin add")
        result = self.run_install(expect=1)
        self.assertEqual(old.read_text(), "old hook\n")
        self.assertNotIn("接线与自检完成", result.stdout)

    def test_codex_cache_preservation_dry_run_does_not_write_or_call_cli(self):
        self.cli("codex")
        old = self.put(".codex/plugins/cache/everos-bridge/everos/0.3.1/everos/hook.py", "old hook\n")
        self.env["FAKECLI_PRUNE_CODEX_CACHE"] = "1"
        self.run_install("--dry-run")
        self.assertEqual(old.read_text(), "old hook\n")
        self.assertEqual(list(old.parents[2].iterdir()), [old.parents[1]])
        self.assertFalse(any(call[0] == "codex" for call in self.calls()))

    def test_codex_old_marketplace_source_is_migrated_and_verified(self):
        self.cli("codex")
        path = self.put(".codex-marketplaces.json", json.dumps({"everos-bridge": "/old/checkout", "other": "/keep"}))
        self.run_install()
        self.assertEqual(json.loads(path.read_text()), {"everos-bridge": str(self.repo), "other": "/keep"})
        calls = self.calls()
        remove = ["codex", "plugin", "marketplace", "remove", "everos-bridge"]
        add = ["codex", "plugin", "marketplace", "add", str(self.repo)]
        self.assertLess(calls.index(remove), calls.index(add))
        self.assertGreaterEqual(calls.count(["codex", "plugin", "marketplace", "list", "--json"]), 2)

    def test_marketplace_success_without_effect_is_failure(self):
        self.cli("codex")
        self.env["FAIL_MIGRATION"] = "1"
        result = self.run_install(expect=1)
        self.assertIn("migration did not take effect", result.stderr)
        self.assertNotIn("接线与自检完成", result.stdout)

    def test_cli_failure_is_not_reported_as_complete(self):
        self.cli("codex")
        self.env["FAIL_ACTION"] = "codex plugin add"
        result = self.run_install(expect=1)
        self.assertNotIn("接线与自检完成", result.stdout)
        self.assertNotIn(["hook-doctor"], self.calls())

    def test_mcp_get_internal_failure_is_not_treated_as_missing(self):
        self.cli("codex")
        self.env["FAIL_ACTION"] = "codex mcp get"
        result = self.run_install(expect=1)
        self.assertIn("simulated host CLI failure", result.stderr)
        self.assertFalse(any(call[:3] == ["codex", "mcp", "add"] for call in self.calls()))

    def test_explicit_mcp_not_found_is_added_without_remove(self):
        self.cli("codex")
        self.env["FAKE_MCP_PRESENT"] = "0"
        self.run_install()
        self.assertFalse(any(call[:3] == ["codex", "mcp", "remove"] for call in self.calls()))
        self.assertTrue(any(call[:3] == ["codex", "mcp", "add"] for call in self.calls()))

    def test_marketplace_remove_failure_is_not_swallowed(self):
        self.cli("codex")
        self.put(".codex-marketplaces.json", json.dumps({"everos-bridge": "/old/checkout"}))
        self.env["FAIL_ACTION"] = "codex plugin marketplace remove"
        self.run_install(expect=1)
        self.assertFalse(any(call[:4] == ["codex", "plugin", "marketplace", "add"] for call in self.calls()))

    def test_coco_existing_mcp_path_is_updated_and_other_rules_survive(self):
        self.cli("coco")
        original = 'model = "keep"\n'
        path = self.put(".trae/traecli.toml", original)
        rules = self.put(".trae/AGENTS.md", "Existing user rules\n")
        self.run_install()
        self.assertIn(["coco", "mcp", "remove", "everos"], self.calls())
        self.assertIn(["coco", "mcp", "add", "everos", "--", "python3", str(self.repo / "plugin/everos/mcp_server.py")], self.calls())
        self.assertEqual(next(path.parent.glob("traecli.toml.bak-everos-*")).read_text(), original)
        self.assertTrue(rules.read_text().startswith("Existing user rules"))
        previous = rules.read_text()
        self.run_install()
        self.assertEqual(rules.read_text(), previous)

    def test_kimi_partial_old_hooks_migrate_preserving_other_hooks(self):
        self.cli("kimi")
        original = ('model = "keep"\n\n[[hooks]]\nevent = "Stop"\ncommand = "python3 /old/plugin/everos/hook.py"\ntimeout = 10\n'
                    '\n[[hooks]]\nevent = "Stop"\ncommand = "echo keep"\ntimeout = 1\n')
        path = self.put(".kimi-code/config.toml", original)
        self.run_install()
        text = path.read_text()
        self.assertNotIn("/old/plugin", text)
        self.assertEqual(text.count("[[hooks]]"), 5)
        self.assertIn('command = "echo keep"', text)
        if tomllib:
            for hook in tomllib.loads(text)["hooks"]:
                if "everos/hook.py" in hook["command"]:
                    self.assertEqual(shlex_split(hook["command"])[1], str(self.repo / "plugin/everos/hook.py"))
        self.run_install()
        self.assertEqual(path.read_text(), text)

    def test_kimi_desktop_missing_home_mcp_is_created(self):
        path = self.put("runtime/config.toml", '[[hooks]]\nevent = "Stop"\ncommand = "EVEROS_HOST=kimi_desktop python3 /old/everos/hook.py"\ntimeout = 10\n')
        self.env["EVEROS_KIMI_DESKTOP_CONFIG"] = str(path)
        self.run_install()
        mcp = json.loads((path.parent / "home/mcp.json").read_text())
        self.assertEqual(mcp["mcpServers"]["everos"]["args"], [str(self.repo / "plugin/everos/mcp_server.py")])
        self.assertEqual(path.read_text().count("EVEROS_HOST=kimi_desktop"), 4)
        previous = path.read_text()
        self.run_install()
        self.assertEqual(path.read_text(), previous)

    def test_claude_old_hook_removed_without_deleting_group_siblings(self):
        self.cli("claude")
        data = {"hooks": {"Stop": [{"matcher": "keep", "hooks": [
            {"type": "command", "command": "python3 /old/everos_hook.py"},
            {"type": "command", "command": "echo keep"}]}]}, "other": True}
        path = self.put(".claude/settings.json", json.dumps(data))
        self.run_install()
        result = json.loads(path.read_text())
        self.assertEqual(result["hooks"]["Stop"], [{"matcher": "keep", "hooks": [{"type": "command", "command": "echo keep"}]}])
        self.assertTrue(result["other"])

    def test_claude_install_is_followed_by_explicit_user_scope_refresh(self):
        self.cli("claude")
        self.run_install()
        install = ["claude", "plugin", "install", "everos@everos-bridge", "--scope", "user"]
        update = ["claude", "plugin", "update", "everos@everos-bridge", "--scope", "user"]
        calls = self.calls()
        self.assertLess(calls.index(install), calls.index(update))
        self.run_install()
        self.assertEqual(self.calls().count(update), 2)

    def test_claude_refresh_failure_is_not_reported_as_complete(self):
        self.cli("claude")
        self.env["FAIL_ACTION"] = "claude plugin update"
        result = self.run_install(expect=1)
        self.assertNotIn("接线与自检完成", result.stdout)
        self.assertNotIn(["hook-doctor"], self.calls())

    def test_json_mcp_merge_preserves_other_servers(self):
        path = self.put(".cursor/mcp.json", '{"mcpServers":{"other":{"command":"keep"}},"theme":"keep"}')
        self.run_install()
        data = json.loads(path.read_text())
        self.assertEqual(data["mcpServers"]["other"], {"command": "keep"})
        self.assertEqual(data["theme"], "keep")
        text = path.read_text()
        self.run_install()
        self.assertEqual(path.read_text(), text)

    def test_claude_desktop_running_is_explicitly_skipped(self):
        self.env["CLAUDE_RUNNING"] = "1"
        path = self.put("Library/Application Support/Claude/claude_desktop_config.json", "{}\n")
        result = self.run_install()
        self.assertEqual(path.read_text(), "{}\n")
        self.assertIn("本次跳过其配置", result.stdout)

    def test_doctor_failure_has_nonzero_exit(self):
        self.env["DOCTOR_EXIT"] = "1"
        result = self.run_install(expect=1)
        self.assertNotIn("接线与自检完成", result.stdout)

    def test_kimi_doctor_failure_has_nonzero_exit(self):
        self.cli("kimi")
        self.env["FAIL_ACTION"] = "kimi doctor"
        self.run_install(expect=1)
        self.assertNotIn(["hook-doctor"], self.calls())

    def test_kimi_cli_falls_back_to_kimi_code_home_bin(self):
        # kimi 不在 PATH，但官方默认位置 ~/.kimi-code/bin/kimi 存在且可执行：
        # 安装不应静默跳过，doctor 用绝对路径调用。
        fallback = self.put(".kimi-code/bin/kimi", "#!" + sys.executable + "\n" + FAKE_CLI)
        fallback.chmod(0o755)
        result = self.run_install()
        self.assertIn("== Kimi Code", result.stdout)
        self.assertIn(["kimi", "doctor"], self.calls())
        config = (self.home / ".kimi-code/config.toml").read_text()
        self.assertIn("everos/hook.py", config)
        mcp = json.loads((self.home / ".kimi-code/mcp.json").read_text())
        self.assertEqual(mcp["mcpServers"]["everos"]["args"], [str(self.repo / "plugin/everos/mcp_server.py")])

    def test_kimi_home_without_executable_cli_warns_and_skips(self):
        # ~/.kimi-code 存在但没有任何可执行的 kimi：给出明确提示而不是静默跳过。
        original = 'model = "keep"\n'
        path = self.put(".kimi-code/config.toml", original)
        result = self.run_install()
        self.assertIn("跳过", result.stdout)
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.home / ".kimi-code/mcp.json").exists())

    def test_kimi_absent_host_stays_silent(self):
        # 完全没装 kimi 的机器与其他未装宿主一致：安静跳过，不刷警告。
        result = self.run_install()
        self.assertNotIn("Kimi Code", result.stdout)

    def test_dry_run_has_no_writes_cli_installs_or_doctor_calls(self):
        for name in ("codex", "claude", "coco", "kimi", "gemini"):
            self.cli(name)
        path = self.put(".codex/config.toml", "[features]\nhooks = false\n")
        before = {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.env["DOCTOR_EXIT"] = "1"
        result = self.run_install("--dry-run")
        after = {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.calls(), [["ps", "-axo", "command"]])
        self.assertIn("未写配置、安装插件或执行 doctor", result.stdout)

    DESKTOP_CONFIG = "Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/config.toml"
    DAIMON_SKILL = "Library/Application Support/kimi-desktop/daimon-share/daimon/skills/everos-memory/SKILL.md"

    def desktop_fixture(self):
        config = self.put(self.DESKTOP_CONFIG, 'model = "keep"\n')
        (self.home / "Library/Application Support/kimi-desktop/daimon-share/daimon/skills").mkdir(parents=True)
        return config

    def test_daimon_skill_installed_then_rerun_is_noop(self):
        self.desktop_fixture()
        result = self.run_install()
        target = self.home / self.DAIMON_SKILL
        self.assertEqual(target.read_text(), "repo skill v1\n")
        self.assertIn("已安装 everos-memory skill", result.stdout)
        result = self.run_install()
        self.assertIn("daimon skill everos-memory 已是最新", result.stdout)
        self.assertEqual(list(target.parent.glob("SKILL.md.bak-everos-*")), [])

    def test_daimon_skill_repo_upgrade_fast_forwards_with_backup(self):
        self.desktop_fixture()
        self.run_install()
        (self.repo / "plugin/skills/everos-memory/SKILL.md").write_text("repo skill v2\n")
        result = self.run_install()
        target = self.home / self.DAIMON_SKILL
        self.assertEqual(target.read_text(), "repo skill v2\n")
        self.assertIn("仓库版 skill 有更新", result.stdout)
        backups = list(target.parent.glob("SKILL.md.bak-everos-*"))
        self.assertEqual([path.read_text() for path in backups], ["repo skill v1\n"])

    def test_daimon_skill_manual_edit_is_reported_and_replaced(self):
        self.desktop_fixture()
        self.run_install()
        target = self.home / self.DAIMON_SKILL
        target.write_text("hand edited\n")
        result = self.run_install()
        self.assertIn("被手改过", result.stdout)
        self.assertEqual(target.read_text(), "repo skill v1\n")
        backups = list(target.parent.glob("SKILL.md.bak-everos-*"))
        self.assertEqual([path.read_text() for path in backups], ["hand edited\n"])

    def test_protocol_symlink_created_healed_and_regular_file_backed_up(self):
        link = self.home / ".everos/PROTOCOL.md"
        self.run_install()
        self.assertEqual(link.read_text(), "# 协议\n")
        # 死链或指向别处：重跑自愈
        link.unlink()
        link.symlink_to("/nonexistent/PROTOCOL.md")
        self.run_install()
        self.assertEqual(link.read_text(), "# 协议\n")
        # 普通文件：备份后替换为软链
        link.unlink()
        link.write_text("stale copy\n")
        self.run_install()
        self.assertEqual(link.read_text(), "# 协议\n")
        backups = [path for path in link.parent.glob("PROTOCOL.md.bak-everos-*")]
        self.assertEqual([path.read_text() for path in backups], ["stale copy\n"])


def shlex_split(command):
    import shlex
    return shlex.split(command)


if __name__ == "__main__":
    unittest.main()
