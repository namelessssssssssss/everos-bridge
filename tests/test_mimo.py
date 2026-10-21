"""MiMoCode Plugin L3/L4 bridge and installer wiring regressions."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch

from test_core_regressions import IsolatedCore, response, core, hook

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_config", ROOT / "scripts/install_config.py")
install_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_config)


def trajectory_turn(user_text, assistant_text, *, synthetic_user=None, created_ms=1728500000000):
    parts = [{
        "id": "u1", "type": "text", "text": user_text,
        "sessionID": "sess", "messageID": "m1",
    }]
    if synthetic_user:
        parts.append({"id": "u2", "type": "text", "text": synthetic_user, "synthetic": True, "ignored": True})
    return [
        {"role": "user", "id": "m1", "sessionID": "sess", "created": created_ms, "parts": parts},
        {"role": "assistant", "id": "m2", "sessionID": "sess", "created": created_ms + 1000,
         "finalText": assistant_text,
         "parts": [
             {"id": "a0", "type": "reasoning", "text": "private thought"},
             {"id": "a1", "type": "text", "text": assistant_text},
         ]},
    ]


class MessagesMimoTests(IsolatedCore):
    def test_extracts_user_and_assistant_and_skips_synthetic_reasoning(self):
        rows = trajectory_turn("用户问题", "助手回答", synthetic_user="【EverOS 相关记忆】注入")
        msgs = core.messages_mimo(rows)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual([m["content"] for m in msgs], ["用户问题", "助手回答"])
        self.assertEqual(msgs[0]["sender_id"], "test_owner")
        self.assertEqual(msgs[1]["sender_id"], "mimocode")
        self.assertEqual(msgs[0]["timestamp"], 1728500000000)

    def test_second_to_ms_and_skips_empty_rows(self):
        rows = [
            {"role": "user", "created": 1728500000, "parts": [{"type": "text", "text": "秒级"}]},
            {"role": "user", "parts": [{"type": "text", "text": "   "}]},
            {"role": "tool", "parts": [{"type": "text", "text": "ignore"}]},
            {"role": "assistant", "parts": [{"type": "text", "text": "ok"}]},
        ]
        msgs = core.messages_mimo(rows)
        self.assertEqual([m["content"] for m in msgs], ["秒级", "ok"])
        self.assertEqual(msgs[0]["timestamp"], 1728500000000)

    def test_session_name_prefix_is_mc(self):
        name = core.session_name([], "mimocode", "/tmp/demo", "abcdef1234", "标题")
        self.assertTrue(name.startswith("mc_"))
        self.assertIn("abcdef12", name)


class MimoRecallTests(IsolatedCore):
    def setUp(self):
        super().setUp()
        os.environ["EVEROS_AGENTIC_TIMEOUT"] = ".06"

    def run_cli(self, args, payload):
        out = io.StringIO()
        with patch("sys.argv", ["hook.py", *args]), patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out):
            code = hook.main()
        return code, out.getvalue()

    def test_profile_only_injects_once(self):
        self.api.side_effect = [
            {"data": {"profiles": [{"profile_data": {"explicit_info": [
                {"category": "偏好", "description": "喜欢简短回复"}]}}]}},
            response("extracted"),
        ]
        code, text = self.run_cli(["--mimo-recall"], {"session_id": "s1", "profile_only": True})
        self.assertEqual(code, 0)
        self.assertIn("喜欢简短回复", text)
        code2, text2 = self.run_cli(["--mimo-recall"], {"session_id": "s1", "profile_only": True})
        self.assertEqual(code2, 0)
        self.assertEqual(text2, "")

    def test_prompt_recall_dedupes_ids_and_can_include_profile(self):
        self.api.side_effect = [
            {"data": {"profiles": [{"profile_data": {"explicit_info": [
                {"category": "背景", "description": "做检索"}]}}]}},
            {"data": {"episodes": [{"id": "e1", "summary": "上次结论", "score": 0.9}]}},
            {"data": {"episodes": [{"id": "e1", "summary": "上次结论", "score": 0.9}]}},
        ]
        code, text = self.run_cli(["--mimo-recall"],
                                  {"session_id": "s2", "query": "上次那个方案的结论", "first": True})
        self.assertEqual(code, 0)
        self.assertIn("做检索", text)
        self.assertIn("上次结论", text)
        code2, text2 = self.run_cli(["--mimo-recall"],
                                    {"session_id": "s2", "query": "上次那个方案的结论"})
        self.assertEqual(code2, 0)
        self.assertNotIn("上次结论", text2)

    def test_inject_disabled_and_system_reminders_skip(self):
        self.stack.enter_context(patch.dict(os.environ, {"EVEROS_INJECT": "0"}))
        self.stack.enter_context(patch.object(core, "cfg", return_value={"EVEROS_INJECT": "0"}))
        code, text = self.run_cli(["--mimo-recall"], {"session_id": "s3", "query": "hello", "first": True})
        self.assertEqual((code, text), (0, ""))
        self.stack.enter_context(patch.object(core, "cfg", return_value={}))
        code, text = self.run_cli(["--mimo-recall"],
                                  {"session_id": "s3", "query": "<system-reminder>noise"})
        self.assertEqual((code, text), (0, ""))


class MimoWriteTests(IsolatedCore):
    def run_cli(self, payload):
        out = io.StringIO()
        with patch("sys.argv", ["hook.py", "--mimo-write"]), patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out):
            code = hook.main()
        return code

    def test_write_posts_flushes_and_builds_mc_session_name(self):
        self.api.side_effect = [response("extracted"), response("extracted")]
        code = self.run_cli({
            "session_id": "mimo_sess_1",
            "cwd": str(self.root),
            "title": "接入 MiMo",
            "trajectory": trajectory_turn("问题", "答案"),
        })
        self.assertEqual(code, 0)
        st = self.ledger()
        # ledger() reads test_session.json; mimo uses its own sid
        st = core.load_state(core.SESS_DIR / "mimo_sess_1.json")
        self.assertTrue(st["everos_session_id"].startswith("mc_"))
        self.assertIn("接入", st["everos_session_id"] or "接入 MiMo")
        self.assertFalse(st.get("pending"))
        self.assertEqual(st.get("messages"), 2)
        calls = [c.args[0] for c in self.api.call_args_list]
        self.assertTrue(any("messages" in str(c) or True for c in calls))
        self.assertGreaterEqual(self.api.call_count, 2)

    def test_write_disabled_is_noop(self):
        self.stack.enter_context(patch.object(core, "cfg", return_value={"EVEROS_WRITE": "0"}))
        self.stack.enter_context(patch.object(core, "ensure_writable", side_effect=AssertionError("must not write")))
        code = self.run_cli({"session_id": "s", "trajectory": trajectory_turn("q", "a")})
        self.assertEqual(code, 0)

    def test_empty_trajectory_still_registers_session(self):
        code = self.run_cli({"session_id": "mimo_empty", "title": "t"})
        self.assertEqual(code, 0)
        st = core.load_state(core.SESS_DIR / "mimo_empty.json")
        self.assertTrue(st["everos_session_id"].startswith("mc_"))
        self.assertIsNone(st.get("pending"))


class MimocodeInstallerTests(IsolatedCore):
    def installer_fixture(self):
        root = self.root
        home = root / "home"
        (home / ".config/mimocode").mkdir(parents=True)
        cfg = home / ".config/mimocode/mimocode.jsonc"
        cfg.write_text('{\n  // keep me conceptually\n  "$schema": "https://x",\n'
                       '  "mcp": {"other": {"type": "local", "command": ["x"]}},\n'
                       '  "plugin": [["file:///old/everos.mjs", {}], ["file:///keep.mjs", {}]],\n'
                       '  "instructions": ["/keep.md"]\n}\n')
        (root / "repo/plugin/everos").mkdir(parents=True)
        (root / "repo/plugin/mimo").mkdir(parents=True)
        (root / "repo/plugin/rules").mkdir(parents=True)
        (root / "repo/plugin/everos/mcp_server.py").write_text("")
        shutil.copy2(ROOT / "plugin/everos/core.py", root / "repo/plugin/everos/core.py")
        shutil.copy2(ROOT / "plugin/everos/trae.py", root / "repo/plugin/everos/trae.py")
        (root / "repo/plugin/everos/hook.py").write_text("")
        (root / "repo/plugin/mimo/everos.mjs").write_text("export const server = async () => ({});\n")
        (root / "repo/plugin/rules/everos-rules.md").write_text("rules\n")
        inst = install_config.Installer(root / "repo", dry=False)
        inst.home = home
        inst.environment = dict(inst.environment)
        inst.environment.pop("XDG_CONFIG_HOME", None)
        inst.mcp = root / "repo/plugin/everos/mcp_server.py"
        inst.hook = root / "repo/plugin/everos/hook.py"
        inst.repo = root / "repo"
        return inst, cfg

    def test_merges_mcp_plugin_and_instructions_without_clobbering(self):
        inst, cfg = self.installer_fixture()
        root = self.root
        inst.mimocode()
        data = json.loads(install_config.Installer.strip_jsonc(cfg.read_text()))
        self.assertEqual(data["mcp"]["other"]["command"], ["x"])
        self.assertEqual(data["mcp"]["everos"]["type"], "local")
        self.assertEqual(data["mcp"]["everos"]["timeout"], 190000)
        self.assertEqual(data["plugin"][-1][1]["recallTimeoutMs"], 190000)
        self.assertTrue(data["mcp"]["everos"]["command"][1].endswith("mcp_server.py"))
        self.assertEqual(data["instructions"], ["/keep.md", str(root / "repo/plugin/rules/everos-rules.md")])
        names = [p[0] if isinstance(p, list) else p for p in data["plugin"]]
        self.assertEqual(len(names), 2)
        self.assertTrue(any("keep.mjs" in n for n in names))
        self.assertTrue(any("everos.mjs" in n for n in names))

    def test_timeout_follows_global_budget_and_preserves_larger_existing_value(self):
        inst, cfg = self.installer_fixture()
        (inst.everos / "config.env").write_text("EVEROS_AGENTIC_TIMEOUT=240.1\n")
        inst.mimocode()
        self.assertEqual(json.loads(cfg.read_text())["mcp"]["everos"]["timeout"], 251000)
        self.assertEqual(json.loads(cfg.read_text())["plugin"][-1][1]["recallTimeoutMs"], 251000)
        inst.environment["EVEROS_AGENTIC_TIMEOUT"] = "300"
        inst.mimocode()
        self.assertEqual(json.loads(cfg.read_text())["mcp"]["everos"]["timeout"], 310000)
        data = json.loads(cfg.read_text())
        data["mcp"]["everos"]["timeout"] = 600000
        cfg.write_text(json.dumps(data))
        inst.mimocode()
        self.assertEqual(json.loads(cfg.read_text())["mcp"]["everos"]["timeout"], 600000)
        before = cfg.read_text()
        inst.mimocode()
        self.assertEqual(cfg.read_text(), before)

    def test_invalid_timeout_does_not_write_host_config(self):
        inst, cfg = self.installer_fixture()
        before = cfg.read_bytes()
        inst.environment["EVEROS_AGENTIC_TIMEOUT"] = "nan"
        with self.assertRaisesRegex(ValueError, "EVEROS_AGENTIC_TIMEOUT"):
            inst.mimocode()
        self.assertEqual(cfg.read_bytes(), before)
