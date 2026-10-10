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
        (self.repo / "plugin/rules").mkdir(parents=True)
        shutil.copy2(ROOT / "install.sh", self.repo / "install.sh")
        shutil.copy2(ROOT / "scripts/install_config.py", self.repo / "scripts/install_config.py")
        (self.repo / "plugin/rules/everos-rules.md").write_text("每条提问检索；每轮对话入库。\n")
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


def shlex_split(command):
    import shlex
    return shlex.split(command)


if __name__ == "__main__":
    unittest.main()
