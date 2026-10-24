"""Native rule migration and source/delta integrity, with no network or real home writes."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_install import install, ROOT
import mcp_server
import rules


class RuleTests(unittest.TestCase):
    def test_managed_update_removes_obsolete_rules_and_preserves_user_text(self):
        old = "user preference\n\n" + rules.BEGIN + "\nobsolete\n" + rules.END + "\nuser footer\n"
        docs = {"rules": "current", "skill": "details"}
        updated = rules.managed_text(old, docs)
        self.assertTrue(updated.startswith("user preference\n\n"))
        self.assertTrue(updated.endswith("\nuser footer\n"))
        self.assertNotIn("obsolete", updated)
        self.assertEqual(rules.managed_text(updated, docs), updated)
        user_text = "\nuser text with trailing spaces  \n\n"
        self.assertTrue(rules.managed_text(user_text, docs).startswith(user_text))
        for damaged in (rules.BEGIN, rules.END, old + old):
            with self.assertRaises(ValueError):
                rules.managed_text(damaged, docs)

    def test_delta_tracks_add_modify_remove_and_survives_noop_install(self):
        first = rules.transition({"rules": "keep\nremove\nold", "skill": "skill"}, {})
        second = rules.transition({"rules": "keep\nnew\nadded", "skill": "skill"}, first)
        self.assertEqual(second["previous_revision"], first["revision"])
        for line in ("-remove", "-old", "+new", "+added"):
            self.assertIn(line, second["changes"])
        self.assertEqual(rules.transition(second["documents"], second), second)

    def test_rule_read_is_paginated_and_does_not_mutate_state_or_require_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(mcp_server.core, "STATE", Path(tmp)), \
                patch.object(mcp_server.core, "ready", return_value=False):
            server = mcp_server.Server()
            first = json.loads(server.call("memory_rules", {"limit": 37}))
            rest = json.loads(server.call("memory_rules", {"offset": first["next_offset"], "limit": 12000}))
            self.assertEqual(first["text"] + rest["text"], rules.bundle(ROOT / "plugin")["rules"])
            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertIn(first["revision"][:12], mcp_server.INSTRUCTIONS)
            self.assertTrue(next(t for t in mcp_server.TOOLS if t["name"] == "memory_rules")["annotations"]["readOnlyHint"])

    def test_old_plugin_reads_own_bundle_and_does_not_claim_new_native_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            new = rules.transition({"rules": "newer", "skill": "newer"}, {})
            (state / "current.json").write_text(json.dumps(new))
            (state / "targets.json").write_text(json.dumps({"revision": new["revision"], "targets": ["new-file"]}))
            result = json.loads(rules.read(ROOT / "plugin", state))
            self.assertNotEqual(result["revision"], new["revision"])
            self.assertFalse(result["source_matches_install"])
            self.assertEqual(result["native_targets"], [])
            self.assertEqual(json.loads((state / "current.json").read_text()), new)

    def installer(self, home, hosts=None, dry=False):
        env = {"EVEROS_INSTALL_HOME": str(home), "EVEROS_HOME": str(home / ".everos"),
               "CODEX_HOME": str(home / ".codex"), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
               "TRAE_HOME": str(home / ".trae"), "KIMI_CODE_HOME": str(home / ".kimi-code"),
               "XDG_CONFIG_HOME": str(home / ".config"),
               "EVEROS_KIMI_DESKTOP_CONFIG": str(home / "Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/config.toml"),
               "EVEROS_KIMI_DESKTOP_HOME": str(home / "Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home")}
        with patch.dict(os.environ, env):
            return install.Installer(ROOT, dry=dry, rule_hosts=hosts)

    def test_all_file_hosts_first_install_readback_then_idempotent_update(self):
        with tempfile.TemporaryDirectory() as tmp, patch("shutil.which", return_value=None):
            home = Path(tmp)
            native = {"codex": ".codex/AGENTS.md", "claude": ".claude/CLAUDE.md", "trae": ".trae/AGENTS.md",
                      "gemini": ".gemini/GEMINI.md", "kimi": ".kimi-code/AGENTS.md", "cursor": ".cursor/rules/everos-bridge.mdc",
                      "kimi_desktop": "Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/AGENTS.md"}
            installer = self.installer(home, list(native))
            installer.sync_rules()
            stamps = {host: (home / name).stat().st_mtime_ns for host, name in native.items()}
            installer.sync_rules()
            revision = rules.revision(rules.bundle(ROOT / "plugin"))
            for host, name in native.items():
                text = (home / name).read_text()
                self.assertIn(revision, text)
                self.assertEqual(text.count(rules.BEGIN), 1)
                self.assertEqual((home / name).stat().st_mtime_ns, stamps[host])
            self.assertIn("alwaysApply: true", (home / native["cursor"]).read_text())
            state = home / ".everos/state/rules"
            if os.name != "nt":
                self.assertEqual((state / "current.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(json.loads((state / "targets.json").read_text())["targets"]), 7)

    def test_codex_override_is_active_target_and_user_rules_preserved_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp, patch("shutil.which", return_value=None):
            home = Path(tmp)
            override = home / ".codex/AGENTS.override.md"
            override.parent.mkdir()
            override.write_text("user override\n")
            normal = override.with_name("AGENTS.md")
            normal.write_text("inactive user rules\n")
            self.installer(home, ["codex"]).sync_rules()
            self.assertTrue(override.read_text().startswith("user override\n"))
            self.assertEqual(normal.read_text(), "inactive user rules\n")
            backups = list(override.parent.glob("AGENTS.override.md.bak-everos-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "user override\n")

    def test_legacy_native_block_is_used_as_partial_baseline_without_inventing_old_skill(self):
        with tempfile.TemporaryDirectory() as tmp, patch("shutil.which", return_value=None):
            home = Path(tmp)
            path = home / ".trae/AGENTS.md"
            path.parent.mkdir()
            path.write_text(rules.BEGIN + "\nlegacy rule\n" + rules.END)
            self.installer(home, ["trae"]).sync_rules()
            state = json.loads((home / ".everos/state/rules/current.json").read_text())
            self.assertIn("-legacy rule", state["changes"])
            self.assertIn("skill_baseline_unavailable", state["baseline"])
            self.assertIsNone(state["previous_revision"])

    def test_dry_run_and_explicit_cloud_host_do_not_need_cli_or_api(self):
        with tempfile.TemporaryDirectory() as tmp, patch("shutil.which", return_value=None):
            home = Path(tmp) / "preview"
            self.installer(home, ["claude"], dry=True).sync_rules()
            self.assertFalse(home.exists())
            self.installer(home, ["claude"]).sync_rules()
            self.assertTrue((home / ".claude/CLAUDE.md").exists())
            self.assertFalse((home / ".codex").exists())

    def test_unowned_cursor_filename_and_symlink_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cursor = home / ".cursor/rules/everos-bridge.mdc"
            cursor.parent.mkdir(parents=True)
            cursor.write_text("user file")
            with self.assertRaises(ValueError):
                self.installer(home, ["cursor"]).sync_rules()
            self.assertEqual(cursor.read_text(), "user file")
            inactive = "---\nalwaysApply: false\n---\n" + rules.managed_text("", rules.bundle(ROOT / "plugin"))
            cursor.write_text(inactive)
            with self.assertRaises(ValueError):
                self.installer(home, ["cursor"]).sync_rules()
            self.assertEqual(cursor.read_text(), inactive)
            target = home / "target"
            target.write_text("user source")
            link = home / "instructions"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                self.installer(home).rules(link)
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_text(), "user source")
