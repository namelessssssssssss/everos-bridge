"""TraeX transcript, incremental collection and lifecycle regressions."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from unittest.mock import patch

from test_core_regressions import IsolatedCore, codex_message, response, core, hook
import trae


def message(role, text, kinds=None, **extra):
    item = codex_message(role, text)["payload"]
    item.update(extra)
    if kinds is not None:
        item["internal_chat_message_metadata_passthrough"] = {"content_item_kinds": kinds}
    return item


def mutation(*items, operation="append", **extra):
    return {"type": "history_mutation", "timestamp": "2026-10-10T02:00:00Z",
            "payload": {"version": 1, "operation": operation, "items": list(items), **extra}}


class TraeTests(IsolatedCore):
    def setUp(self):
        super().setUp()
        self.runtime = self.root.resolve() / "runtime"
        self.stack.enter_context(patch.dict(os.environ, {"TRAECLI_HOME": str(self.runtime), "EVEROS_HOST": "trae"}))
        self.stack.enter_context(patch.object(hook, "HOST_OVERRIDE", "trae"))

    def rollout(self, entries):
        path = self.runtime / "sessions/2026/10/10/rollout-2026-10-10T10-00-00-test_session.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        head = {"type": "session_meta", "timestamp": "2026-10-10T02:00:00Z",
                "payload": {"id": "test_session", "originator": "traex-tui"}}
        path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in [head, *entries]))
        return path

    def payload(self, event="Stop"):
        return {"session_id": "test_session", "transcript_path": None, "hook_event_name": event,
                "thread_name": "TraeX integration", "cwd": str(self.root)}

    def lifecycle(self, event, **kwargs):
        payload = dict(self.payload(event), **kwargs)
        out = io.StringIO()
        with patch("sys.argv", ["hook.py"]), patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            self.assertEqual(hook.main(), 0)
        return out.getvalue()

    def test_modern_dialogue_excludes_scaffolding_tools_and_ui_mirrors(self):
        user = message("user", "environment", ["environments.environment_context", "user.text"])
        user["content"].append({"type": "input_text", "text": "用户原文"})
        rows = [mutation(message("developer", "system"), user),
                mutation(message("assistant", "进度", phase="commentary")),
                mutation({"type": "function_call", "name": "x", "arguments": "{}"},
                         {"type": "reasoning", "text": "private"}),
                mutation(message("assistant", "analysis", channel="analysis")),
                mutation(message("assistant", "结果", phase="final_answer"), display_completions=[
                    {"item": {"type": "AgentMessage", "text": "结果"}}]),
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "结果"}},
                mutation(message("user", "compaction snapshot"), operation="replace")]
        msgs = core.messages_trae(rows)
        self.assertEqual([m["content"] for m in msgs], ["用户原文", "进度", "结果"])
        self.assertEqual([m["sender_id"] for m in msgs], ["test_owner", "trae", "trae"])
        self.assertLess(msgs[0]["timestamp"], msgs[1]["timestamp"])

    def test_legacy_response_items_and_host_identity(self):
        rows = [codex_message("user", "<environment_context>setup"), codex_message("user", "question"),
                codex_message("assistant", "answer", "final")]
        self.assertEqual([m["content"] for m in core.messages_trae(rows)], ["question", "answer"])
        self.assertEqual(core.detect_host(rows, "/home/u/.trae/cli/sessions/x.jsonl"), "trae")
        self.assertEqual(core.detect_host([{"type": "session_meta", "payload": {"originator": "traex-exec"}}]), "trae")
        self.assertEqual(core.detect_host(rows, "/home/u/.codex/sessions/x.jsonl"), "codex")

    def test_explicit_user_text_preserves_scaffold_like_content_and_long_messages(self):
        rows = [mutation(message("user", "# AGENTS.md 用户自己提供的内容", ["user.text"])),
                mutation(message("assistant", "a" * 15000), message("assistant", "b" * 15000))]
        msgs = core.messages_trae(rows)
        self.assertEqual(msgs[0]["content"], "# AGENTS.md 用户自己提供的内容")
        self.assertEqual([len(m["content"]) for m in msgs[1:]], [15000, 15000])

    def test_runtime_home_respects_both_host_directories(self):
        with patch.dict(os.environ, {"TRAE_HOME": str(self.root / "shared"), "USERPROFILE": str(self.root)}, clear=True):
            self.assertEqual(trae.runtime_home(), (self.root / "shared/cli").resolve())
        self.assertEqual(trae.runtime_home(), self.runtime)

    def test_uuidv7_sessions_with_same_time_prefix_stay_separate(self):
        first = core.session_name([], "trae", "/workspace", "01a125ce-1111-7000-8000-000000000001")
        second = core.session_name([], "trae", "/workspace", "01a125ce-1111-7000-8000-000000000002")
        self.assertNotEqual(first, second)
        self.assertLess(len(first), 128)

    def test_null_transcript_path_resolves_session_and_writes_once(self):
        path = self.rollout([mutation(message("user", "question", ["user.text"]), message("assistant", "answer"))])
        core.write_turns(self.payload())
        self.assertEqual(self.ledger()["host"], "trae")
        self.assertTrue(self.ledger()["everos_session_id"].startswith("tx_20261010_TraeX_integration_"))
        self.assertEqual(self.ledger()["offset"], path.stat().st_size)
        self.assertEqual(self.ledger()["messages"], 2)
        self.api.reset_mock()
        core.write_turns(self.payload())
        self.api.assert_not_called()

    def test_read_only_index_lookup_and_filename_fallback(self):
        path = self.rollout([])
        db = self.runtime / "state_5.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
            conn.execute("INSERT INTO threads VALUES (?, ?)", ("test_session", str(path)))
        conn.close()  # Connection context commits but does not close the Windows handle.
        self.assertEqual(trae.transcript("test_session"), path)
        db.write_text("bad index")
        self.assertEqual(trae.transcript("test_session"), path)
        with self.assertRaises(ValueError):
            trae.transcript("../../x")
        self.assertIsNone(trae.transcript("missing"))

    def test_missing_rollout_does_not_create_or_advance_ledger(self):
        with self.assertRaises(FileNotFoundError):
            core.write_turns(self.payload())
        self.assertEqual(self.ledger(), {})

    def test_api_failure_retries_and_partial_unicode_line_waits_for_completion(self):
        path = self.rollout([mutation(message("user", "首轮", ["user.text"]))])
        self.api.return_value = response("failed")
        with self.assertRaises(core.APIResponseError):
            core.write_turns(self.payload())
        self.assertEqual(self.ledger().get("offset", 0), 0)
        self.api.return_value = response("accumulated")
        core.write_turns(self.payload())
        offset = self.ledger()["offset"]
        data = json.dumps(mutation(message("assistant", "完整答复")), ensure_ascii=False).encode()
        with path.open("ab") as stream:
            stream.write(data[:-2])
        self.api.reset_mock()
        core.write_turns(self.payload())
        self.api.assert_not_called()
        self.assertEqual(self.ledger()["offset"], offset)
        with path.open("ab") as stream:
            stream.write(data[-2:] + b"\n")
        core.write_turns(self.payload())
        self.assertEqual(self.ledger()["messages"], 2)
        self.assertEqual(self.api.call_args.args[1]["messages"][0]["content"], "完整答复")

    def test_unknown_version_or_bad_metadata_does_not_consume_offset(self):
        row = mutation(message("user", "question", []))
        self.rollout([row])
        with self.assertRaises(ValueError):
            core.write_turns(self.payload())
        self.assertEqual(self.ledger(), {})
        row["payload"]["version"] = 99
        with self.assertRaises(ValueError):
            core.messages_trae([row])

    def test_overlapping_stop_and_session_end_do_not_duplicate_messages(self):
        self.rollout([mutation(message("user", "one turn"), message("assistant", "done"))])
        errors = []
        def run(event):
            try:
                core.write_turns(self.payload(event))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(event,)) for event in ["Stop", "SessionEnd"]]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(errors, [])
        adds = [call for call in self.api.call_args_list if call.args[0].endswith("/add")]
        self.assertEqual(len(adds), 1)
        self.assertEqual(self.ledger()["messages"], 2)
        self.assertFalse(self.ledger()["pending"])

    def test_disabled_writes_and_fail_open_lifecycle(self):
        self.rollout([mutation(message("user", "question"))])
        with patch.dict(os.environ, {"EVEROS_WRITE": "0"}), patch.object(hook.subprocess, "Popen") as proc:
            self.lifecycle("Stop")
            proc.assert_not_called()
        with patch.object(core, "profile_text", side_effect=RuntimeError("offline")):
            self.assertEqual(self.lifecycle("SessionStart"), "")
        self.assertIn("offline", core.LOG.read_text())

    def test_profile_once_then_postcompact_restores_on_next_prompt(self):
        with patch.object(core, "profile_text", return_value="profile") as profile, patch.object(core, "search_items", return_value=[]):
            start = json.loads(self.lifecycle("SessionStart"))
            self.assertIn("用户画像", start["hookSpecificOutput"]["additionalContext"])
            self.lifecycle("UserPromptSubmit", prompt="first question")
            self.assertEqual(profile.call_count, 1)
            self.assertEqual(self.lifecycle("PostCompact"), "")
            reply = json.loads(self.lifecycle("UserPromptSubmit", prompt="next question"))
            self.assertIn("用户画像", reply["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(profile.call_count, 2)

    def test_first_prompt_profile_fallback_and_recall_share_deadline(self):
        def profile(**kwargs):
            time.sleep(.035)
            return "fallback profile"
        with patch.dict(os.environ, {"EVEROS_AGENTIC_TIMEOUT": ".06"}), \
                patch.object(core, "profile_text", side_effect=profile), \
                patch.object(core, "search_items", return_value=[]) as search:
            self.lifecycle("UserPromptSubmit", prompt="fallback question")
        self.assertLess(search.call_args.kwargs["budget"], .04)

    def test_duplicate_user_and_project_hooks_inject_only_once(self):
        with patch.object(core, "profile_text", return_value="profile") as profile, patch.object(core, "search_items", return_value=[
                {"id": "one", "text": "remember this", "kind": "episode"}]) as search:
            self.assertTrue(self.lifecycle("SessionStart", source="startup"))
            self.assertEqual(self.lifecycle("SessionStart", source="startup"), "")
            self.assertEqual(profile.call_count, 1)
            self.assertTrue(self.lifecycle("UserPromptSubmit", prompt="question", turn_id="turn1"))
            self.assertEqual(self.lifecycle("UserPromptSubmit", prompt="question", turn_id="turn1"), "")
            self.assertEqual(search.call_count, 1)
            self.lifecycle("UserPromptSubmit", prompt="new input in same turn", turn_id="turn1")
            self.assertEqual(search.call_count, 2)
            self.lifecycle("UserPromptSubmit", prompt="question", turn_id="turn2")
            self.assertEqual(search.call_count, 3)
            self.lifecycle("SessionStart", source="resume")
            self.assertEqual(profile.call_count, 2)
