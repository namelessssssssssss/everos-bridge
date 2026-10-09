"""Regression coverage with temporary state and no real EverOS requests.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "plugin" / "everos"
sys.path.insert(0, str(SOURCE))
import core
import hook


def response(status):
    return {"data": {"status": status}}


def codex_message(role, text, channel=None):
    return {"type": "response_item", "timestamp": "2026-10-08T00:00:00Z",
            "payload": {"type": "message", "role": role, "channel": channel,
                        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}


class IsolatedCore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "EVEROS_HOME": str(self.root), "EVEROS_USER_ID": "test_owner",
            "EVEROS_API_KEY": "test_key", "EVEROS_API_VERSION": "v2", "EVEROS_READ_V1": "0",
            "EVEROS_WRITE": "1", "EVEROS_RECALL_TIMEOUT": ".06", "EVEROS_RECALL_CHARS": "2000",
        }))
        for name, path in {
            "HOME": self.root, "STATE": self.root / "state", "SESS_DIR": self.root / "state/sessions",
            "RECALL_DIR": self.root / "state/recall", "LOG": self.root / "state/hook.log",
            "SWEEP_MARK": self.root / "state/sweep", "LOCALMEM_ROOT": self.root / "projects",
            "LOCALMEM_LEDGER": self.root / "state/localmem.json",
        }.items():
            self.stack.enter_context(patch.object(core, name, path))
        self.net = self.stack.enter_context(patch.object(core.urllib.request, "urlopen",
                                                       side_effect=AssertionError("Real network is forbidden")))
        self.real_api = core.api
        self.api = self.stack.enter_context(patch.object(core, "api", return_value=response("extracted")))
        self.stack.enter_context(patch.object(core, "sweep"))

    def transcript(self, text="retry this message"):
        path = self.root / "transcript.jsonl"
        path.write_text(json.dumps(codex_message("user", text)) + "\n", encoding="utf-8")
        return {"session_id": "test_session", "transcript_path": str(path), "hook_event_name": "Stop"}

    def ledger(self):
        return core.load_state(core.SESS_DIR / "test_session.json")

    def local_note(self):
        folder = core.LOCALMEM_ROOT / "project/memory"
        folder.mkdir(parents=True)
        note = folder / "note.md"
        note.write_text("first version", encoding="utf-8")
        return note


class ResponseAndWriteTests(IsolatedCore):
    def test_http200_business_error_is_rejected_by_api(self):
        self.net.side_effect = None
        self.net.return_value = io.BytesIO(b'{"code":500,"message":"write rejected"}')
        with self.assertRaisesRegex(core.APIResponseError, "write rejected"):
            self.real_api("/api/v2/memory/add", {})

    def test_add_failures_leave_offset_retryable(self):
        payload = self.transcript()
        for out in [{"code": 500, "data": {"status": "extracted"}}, response("unknown"), response([]), {}]:
            with self.subTest(out=out):
                self.api.return_value = out
                with self.assertRaises(core.APIResponseError):
                    core.write_turns(payload)
                self.assertEqual(self.ledger().get("offset", 0), 0)
        self.api.return_value = response("extracted")
        core.write_turns(payload)
        self.assertGreater(self.ledger()["offset"], 0)
        self.assertEqual(self.ledger()["messages"], 1)

    def test_add_dedup_status_is_accepted_and_still_requires_flush_confirmation(self):
        payload = self.transcript()
        self.api.return_value = response("no_extraction")
        core.write_turns(payload)
        self.assertGreater(self.ledger()["offset"], 0)
        self.assertTrue(self.ledger()["pending"])
        payload["hook_event_name"] = "SessionEnd"
        core.write_turns(payload)
        self.assertFalse(self.ledger()["pending"])

    def test_partial_batch_failure_retries_without_advancing_offset(self):
        payload = self.transcript("one\n" * 100)
        path = Path(payload["transcript_path"])
        path.write_text("".join(json.dumps(codex_message("user", str(i))) + "\n" for i in range(401)))
        self.api.side_effect = [response("accumulated"), {"code": 500, "message": "second batch rejected"}]
        with self.assertRaises(core.APIResponseError):
            core.write_turns(payload)
        self.assertEqual(self.ledger().get("offset", 0), 0)
        self.api.side_effect = None
        core.write_turns(payload)
        self.assertEqual(self.ledger()["messages"], 401)

    def test_flush_failure_keeps_pending_and_retries(self):
        payload = self.transcript()
        core.write_turns(payload)
        offset = self.ledger()["offset"]
        payload["hook_event_name"] = "SessionEnd"
        for out in [response("buffered"), {"code": 500}, {}]:
            self.api.return_value = out
            core.write_turns(payload)
            self.assertTrue(self.ledger()["pending"])
            self.assertEqual(self.ledger()["offset"], offset)
        self.api.return_value = response("no_extraction")
        core.write_turns(payload)
        self.assertFalse(self.ledger()["pending"])
        self.assertEqual(self.ledger()["messages"], 1)

    def test_failed_old_version_flush_does_not_lose_old_pending(self):
        payload = self.transcript()
        old = {"everos_session_id": "old", "api": "v1", "pending": True, "offset": 0, "host": "codex"}
        core.save_state(core.SESS_DIR / "test_session.json", old)
        self.api.return_value = {"code": 500, "message": "old flush rejected"}
        with self.assertRaises(core.APIResponseError):
            core.write_turns(payload)
        self.assertEqual(self.ledger(), old)
        self.api.assert_called_once()
        self.assertEqual(self.api.call_args.args[0], "/api/v1/memories/flush")
        self.api.reset_mock()
        self.api.return_value = response("extracted")
        core.write_turns(payload)
        self.assertEqual(self.ledger()["api"], "v2")
        self.assertEqual([c.args[0] for c in self.api.call_args_list], ["/api/v1/memories/flush", "/api/v2/memory/add"])

    def test_all_write_entry_points_obey_disabled_switch(self):
        payload = self.transcript()
        with patch.dict(os.environ, {"EVEROS_WRITE": "0"}):
            actions = [lambda: core.post_messages("s", [{"role": "user", "content": "x"}]),
                       lambda: core.flush("s", ver="v1"),
                       lambda: core.remember("codex", "topic", "note"),
                       lambda: core.record_practice("codex", "topic", "task", [], "result"),
                       lambda: core.fix_profile([{"action": "add"}]),
                       lambda: core.write_turns(payload), lambda: core.sync_local_memory()]
            for action in actions:
                with self.assertRaisesRegex(RuntimeError, "EVEROS_WRITE=0"):
                    action()
        self.api.assert_not_called()
        self.assertFalse(core.LOCALMEM_LEDGER.exists())

    def test_practice_failure_retains_dual_write_outcomes(self):
        with patch.dict(os.environ, {"EVEROS_READ_V1": "1"}), patch.object(core.time, "sleep"):
            self.api.side_effect = [response("extracted"), response("no_extraction"), RuntimeError("v1 offline")]
            with self.assertRaisesRegex(RuntimeError, "v2 extracted/no_extraction; v1 failed"):
                core.record_practice("codex", "partial", "task", [], "conclusion")
            self.assertIn("v2 extracted/no_extraction; v1 failed", core.LOG.read_text())
            self.api.side_effect = RuntimeError("all writes offline")
            with self.assertRaisesRegex(RuntimeError, "v2 failed.*v1 failed"):
                core.record_practice("codex", "all", "task", [], "conclusion")
            self.api.side_effect = [response("extracted"), RuntimeError("v2 flush offline"),
                                    response("accumulated"), response("extracted")]
            with self.assertRaisesRegex(RuntimeError, "add=extracted，flush 未完成.*v1 accumulated/extracted"):
                core.record_practice("codex", "flush_failed", "task", [], "conclusion")

    def test_profile_edit_never_reports_partial_application_as_success(self):
        ops = [{"action": "add"}, {"action": "update"}]
        for out in [{"code": 500}, {"data": {"applied": 1}},
                    {"data": {"applied": 2, "results": [{"status": "failed", "error": "invalid"}]}}, {}]:
            self.api.return_value = out
            with self.assertRaises(core.APIResponseError):
                core.fix_profile(ops)
        self.api.return_value = {"data": {"applied": 2, "results": [{"status": "applied"}, {"status": "applied"}]}}
        self.assertEqual(json.loads(core.fix_profile(ops))["applied"], 2)

    def test_codex_full_collection_preserves_existing_analysis_policy(self):
        msgs = core.messages_codex([codex_message("user", "question"),
                                    codex_message("assistant", "full record", "analysis"),
                                    codex_message("assistant", "answer", "final")])
        self.assertIn("full record", msgs[-1]["content"])
        self.assertIn("answer", msgs[-1]["content"])


class RecallTests(IsolatedCore):
    def test_top_k_reaches_user_agent_and_legacy_queries_without_clamping(self):
        for top_k in (-1, 1, 21, 100):
            with self.subTest(top_k=top_k), patch.dict(os.environ, {"EVEROS_READ_V1": "1"}), \
                    patch.object(core, "_read_jobs", return_value={}) as read:
                core.search_items("query", top_k=top_k)
                jobs = read.call_args.args[0]
                self.assertEqual(set(jobs), {"v2u", "v2a", "v1"})
                self.assertTrue(all(body["top_k"] == top_k for _, body in jobs.values()))

    def test_invalid_top_k_fails_before_any_query_is_started(self):
        with patch.object(core, "_read_jobs") as read:
            for top_k in (-2, 0, 101, True, 1.5, "5"):
                with self.subTest(top_k=top_k), self.assertRaisesRegex(ValueError, "top_k"):
                    core.search_items("query", top_k=top_k)
            read.assert_not_called()

    def test_server_sized_legacy_results_keep_the_last_item_and_hook_cap(self):
        episodes = [{"id": str(i), "summary": f"legacy result {i}"} for i in range(5)]
        with patch.dict(os.environ, {"EVEROS_READ_V1": "1"}), \
                patch.object(core, "_read_jobs", return_value={"v1": {"episodes": episodes}}):
            items = core.search_items("query", top_k=-1)
            self.assertEqual([it["id"] for it in items], [str(i) for i in range(5)])
            capped = core.search_items("query", top_k=-1, v1_episodes=3)
            self.assertEqual([it["id"] for it in capped], ["0", "1", "2"])

    def test_multiline_ids_only_cover_actual_items_and_hidden_item_can_reappear(self):
        items = [{"id": "shown", "kind": "episode", "text": "first\nsecond\nthird"},
                 {"id": "hidden", "kind": "episode", "text": "x" * 60}]
        payload = {"session_id": "multi", "prompt": "query"}
        with patch.dict(os.environ, {"EVEROS_RECALL_CHARS": "35"}), patch.object(core, "search_items", return_value=items):
            with contextlib.redirect_stdout(io.StringIO()):
                hook.on_prompt(payload)
        state = core.RECALL_DIR / "multi.json"
        self.assertEqual(core.load_state(state)["ids"], ["shown"])
        with patch.object(core, "search_items", return_value=items), contextlib.redirect_stdout(io.StringIO()) as out:
            hook.on_prompt(payload)
        self.assertIn("x" * 60, out.getvalue())
        self.assertNotIn("first", out.getvalue())
        self.assertEqual(core.load_state(state)["ids"], ["hidden", "shown"])

    def test_zero_budget_does_not_start_network_work(self):
        self.assertEqual(core.search_items("query", budget=0), [])
        self.assertEqual(core.profile_text(budget=0), "")
        self.api.assert_not_called()

    def test_session_start_profile_budget_fits_host_timeout(self):
        payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "start"})
        with patch.dict(os.environ, {"EVEROS_RECALL_TIMEOUT": "18"}), patch.object(sys, "argv", ["hook.py"]), \
                patch.object(sys, "stdin", io.StringIO(payload)), patch.object(core, "profile_text", return_value="profile") as profile, \
                patch.object(hook, "HOST_OVERRIDE", ""), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hook.main(), 0)
        self.assertEqual(profile.call_args.kwargs["budget"], core.READ_TIMEOUT)
        self.assertIn("profile", out.getvalue())

    def test_deadline_keeps_fast_results_without_waiting_for_slow_track(self):
        def fake_api(path, body, timeout):
            if body.get("user_id"):
                return {"data": {"episodes": [{"id": "fast", "summary": "fast result"}]}}
            time.sleep(.5)
            return {"data": {"agent_cases": [{"id": "late", "score": .9, "task_intent": "late"}]}}
        self.api.side_effect = fake_api
        start = time.monotonic()
        items = core.search_items("query", budget=.04)
        self.assertLess(time.monotonic() - start, .25)
        self.assertEqual([it["id"] for it in items], ["fast"])

    def test_kimi_profile_and_recall_share_one_budget(self):
        def fake_api(path, body, timeout):
            time.sleep(.5)
            return {"data": {}}
        self.api.side_effect = fake_api
        start = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            hook.on_prompt({"session_id": "kimi_first", "prompt": "meaningful question"}, kimi=True)
        self.assertLess(time.monotonic() - start, .25)
        # The slow first profile exhausted this round's budget; no search gets a fresh timeout.
        self.assertTrue(all(c.args[0].endswith("/get") for c in self.api.call_args_list))

    def test_subprocess_exits_without_waiting_for_timed_out_workers(self):
        script = "\n".join([
            "import sys, time", f"sys.path.insert(0, {str(SOURCE)!r})", "import core, hook",
            "def slow(*args, **kwargs):", "    time.sleep(2)", "    return {'data': {}}", "core.api = slow",
            "hook.on_prompt({'session_id': 'process', 'prompt': 'question'}, kimi=True)", "print('finished')",
        ])
        start = time.monotonic()
        out = subprocess.run([sys.executable, "-c", script], env=dict(os.environ), capture_output=True,
                             text=True, timeout=1.5)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("finished", out.stdout)
        self.assertLess(time.monotonic() - start, 1.0)


class LocalMemoryTests(IsolatedCore):
    def test_new_file_version_retries_after_three_incomplete_flushes(self):
        note = self.local_note()
        self.api.return_value = response("buffered")
        with patch.object(core.time, "sleep"):
            for _ in range(4):
                core.sync_local_memory()
        self.assertEqual(self.api.call_count, 6)
        self.assertEqual(core.load_state(core.LOCALMEM_LEDGER)[str(note)]["attempts"], 3)
        old_mtime = note.stat().st_mtime
        note.write_text("new version", encoding="utf-8")
        os.utime(note, (old_mtime + 60, old_mtime + 60))
        self.api.reset_mock()
        self.api.return_value = response("extracted")
        with patch.object(core.time, "sleep"):
            core.sync_local_memory()
            core.sync_local_memory()
        self.assertEqual(self.api.call_count, 2)
        st = core.load_state(core.LOCALMEM_LEDGER)[str(note)]
        self.assertEqual(st["attempts"], 0)
        self.assertEqual(st["mtime"], note.stat().st_mtime)

    def test_network_errors_do_not_permanently_disable_same_content(self):
        note = self.local_note()
        self.api.side_effect = TimeoutError("network unavailable")
        for _ in range(4):
            core.sync_local_memory()
        self.assertEqual(self.api.call_count, 4)
        self.assertEqual(core.load_state(core.LOCALMEM_LEDGER)[str(note)]["attempts"], 0)
        self.api.side_effect = None
        self.api.reset_mock()
        with patch.object(core.time, "sleep"):
            core.sync_local_memory()
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(core.load_state(core.LOCALMEM_LEDGER)[str(note)]["mtime"], note.stat().st_mtime)

    def test_no_extraction_flush_is_completed_dedup(self):
        note = self.local_note()
        self.api.side_effect = [response("accumulated"), response("no_extraction")]
        with patch.object(core.time, "sleep"):
            core.sync_local_memory()
            core.sync_local_memory()
        self.assertEqual(self.api.call_count, 2)
        st = core.load_state(core.LOCALMEM_LEDGER)[str(note)]
        self.assertEqual(st["mtime"], note.stat().st_mtime)
        self.assertEqual(st["attempts"], 0)

    def test_old_failure_ledger_without_version_migrates_to_retryable_state(self):
        note = self.local_note()
        core.save_state(core.LOCALMEM_LEDGER, {str(note): {"mtime": 0, "attempts": 3}})
        with patch.object(core.time, "sleep"):
            core.sync_local_memory()
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(core.load_state(core.LOCALMEM_LEDGER)[str(note)]["attempted_mtime"], note.stat().st_mtime)


class DoctorTests(IsolatedCore):
    def test_doctor_retains_readable_report_and_missing_config_is_unhealthy(self):
        with patch.dict(os.environ, {"EVEROS_USER_ID": ""}):
            report, healthy = core.doctor_result()
            self.assertFalse(healthy)
            self.assertIn("缺失：设置 EVEROS_USER_ID", report)
            self.assertEqual(core.doctor(), report)
        self.api.assert_not_called()

    def test_doctor_checks_every_transition_endpoint_and_propagates_partial_failure(self):
        with patch.dict(os.environ, {"EVEROS_READ_V1": "1"}):
            self.api.side_effect = [{"data": {"total_count": 1}}, {"code": 500, "message": "case failure"},
                                    {"data": {"total_count": 2}}]
            report, healthy = core.doctor_result()
        self.assertFalse(healthy)
        self.assertEqual(self.api.call_count, 3)
        self.assertEqual(report.count("OK"), 2)
        self.assertIn("case failure", report)
        self.assertNotIn("test_key", report)

    def test_doctor_success_and_failure_have_reliable_subprocess_exit_codes(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                script = "\n".join([
                    "import sys", f"sys.path.insert(0, {str(SOURCE)!r})", "import core, hook",
                    "def mock_api(*args, **kwargs):",
                    "    return " + ("{'code': 500, 'message': 'diagnostic failure'}" if failure else
                                      "{'data': {'total_count': 0}}"),
                    "core.api = mock_api", "sys.argv = ['hook.py', '--doctor']", "sys.exit(hook.main())",
                ])
                out = subprocess.run([sys.executable, "-c", script], env=dict(os.environ), capture_output=True,
                                     text=True, timeout=2)
                self.assertEqual(out.returncode, 1 if failure else 0, out.stderr)
                self.assertIn("失败：" if failure else "OK", out.stdout)
                self.assertNotIn("test_key", out.stdout + out.stderr)

    def test_unexpected_doctor_exception_is_nonzero_but_lifecycle_keeps_fail_open(self):
        with patch.object(sys, "argv", ["hook.py", "--doctor"]), \
                patch.object(core, "doctor_result", side_effect=RuntimeError("unexpected doctor failure")):
            self.assertEqual(hook.main(), 1)
        with patch.object(sys, "argv", ["hook.py"]), patch.object(sys, "stdin", io.StringIO("{bad json")):
            self.assertEqual(hook.main(), 0)


if __name__ == "__main__":
    unittest.main()
