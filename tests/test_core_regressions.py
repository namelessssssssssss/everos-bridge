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
        self.stack.enter_context(patch.dict(os.environ))
        os.environ.pop("EVEROS_AGENTIC_TIMEOUT", None)
        os.environ.pop("EVEROS_CASE_WAIT", None)
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
        steps = [{"action": f"act {i}", "result": f"result {i}"} for i in range(3)]
        with patch.dict(os.environ, {"EVEROS_READ_V1": "1"}), patch.object(core.time, "sleep"):
            self.api.side_effect = [response("extracted"), response("no_extraction"), RuntimeError("v1 offline")]
            with self.assertRaisesRegex(RuntimeError, "v2 extracted/no_extraction; v1 failed"):
                core.record_practice("codex", "partial", "task", steps, "conclusion")
            self.assertIn("v2 extracted/no_extraction; v1 failed", core.LOG.read_text())
            self.api.side_effect = RuntimeError("all writes offline")
            with self.assertRaisesRegex(RuntimeError, "v2 failed.*v1 failed"):
                core.record_practice("codex", "all", "task", steps, "conclusion")
            self.api.side_effect = [response("extracted"), RuntimeError("v2 flush offline"),
                                    response("accumulated"), response("extracted")]
            with self.assertRaisesRegex(RuntimeError, "add=extracted，flush 未完成.*v1 accumulated/extracted"):
                core.record_practice("codex", "flush_failed", "task", steps, "conclusion")
        # 写入失败时不做 case 回读：只有上面列出的 add / flush 调用。
        self.assertNotIn("/api/v2/memory/get", [c.args[0] for c in self.api.call_args_list])

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


class AgenticRecallTests(IsolatedCore):
    @staticmethod
    def episode(identifier="episode", text="the direct historical answer"):
        return {"id": identifier, "summary": text, "score": .02}

    @staticmethod
    def case(identifier="case", score=.02):
        return {"id": identifier, "task_intent": identifier, "key_insight": "a useful practice", "score": score}

    def test_agentic_timeout_defaults_to_180_and_can_parse_without_loading_configuration(self):
        with patch.object(core, "cfg", side_effect=AssertionError("Configuration read is forbidden")):
            self.assertEqual(core.agentic_timeout({}), 180)
            self.assertEqual(core.agentic_timeout({"EVEROS_AGENTIC_TIMEOUT": "240.5"}), 240.5)

    def test_agentic_timeout_accepts_positive_finite_seconds(self):
        for value in ("0.25", "180", "300.5", " 240 ", 180, 1.5):
            with self.subTest(value=value):
                self.assertEqual(core.agentic_timeout({"EVEROS_AGENTIC_TIMEOUT": value}), float(value))

    def test_agentic_timeout_rejects_invalid_values(self):
        for value in ("", " ", "abc", "0", "-1", "nan", "inf", "-inf",
                      0, -1, 10 ** 400, float("nan"), float("inf"), None, True, False, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "EVEROS_AGENTIC_TIMEOUT"):
                core.agentic_timeout({"EVEROS_AGENTIC_TIMEOUT": value})

    def test_agentic_timeout_reads_config_file_and_environment_takes_precedence(self):
        (self.root / "config.env").write_text("EVEROS_AGENTIC_TIMEOUT=240.5\n", encoding="utf-8")
        self.assertEqual(core.agentic_timeout(), 240.5)
        with patch.dict(os.environ, {"EVEROS_AGENTIC_TIMEOUT": "300"}):
            self.assertEqual(core.agentic_timeout(), 300)
        with patch.dict(os.environ, {"EVEROS_AGENTIC_TIMEOUT": "invalid"}):
            with self.assertRaisesRegex(ValueError, "EVEROS_AGENTIC_TIMEOUT"):
                core.agentic_timeout()

    def test_invalid_agentic_timeout_fails_before_starting_network_work(self):
        (self.root / "config.env").write_text("EVEROS_AGENTIC_TIMEOUT=nan\n", encoding="utf-8")
        with patch.object(core, "_read_jobs") as read:
            with self.assertRaisesRegex(ValueError, "EVEROS_AGENTIC_TIMEOUT"):
                core.search_items("history", method="agentic")
            read.assert_not_called()
        self.api.assert_not_called()

    def test_explicit_budget_overrides_invalid_agentic_configuration(self):
        budgets = []
        clock = [100.0]

        def consume_budget(jobs, budget):
            budgets.append(budget)
            clock[0] += budget
            return {name: {} for name in jobs}

        with patch.dict(os.environ, {"EVEROS_AGENTIC_TIMEOUT": "invalid"}), \
                patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(core, "_read_jobs", side_effect=consume_budget):
            core.search_items("history", method="agentic", budget=9)
        self.assertEqual(budgets, [6, 3])
        self.assertEqual(clock[0], 109)

    def test_configured_agentic_budget_and_fallback_share_the_total_deadline(self):
        (self.root / "config.env").write_text("EVEROS_AGENTIC_TIMEOUT=240\n", encoding="utf-8")
        budgets = []
        clock = [100.0]

        def consume_budget(jobs, budget):
            budgets.append(budget)
            clock[0] += budget
            return {name: {} for name in jobs}

        with patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(core, "_read_jobs", side_effect=consume_budget):
            core.search_items("history", method="agentic")
        self.assertEqual(budgets, [228, 12])
        self.assertEqual(clock[0], 340)

    def test_hybrid_and_v1_deep_search_ignore_agentic_timeout_configuration(self):
        with patch.dict(os.environ, {"EVEROS_AGENTIC_TIMEOUT": "invalid"}), \
                patch.object(core.time, "monotonic", return_value=100), \
                patch.object(core, "_read_jobs", side_effect=lambda jobs, budget: {name: {} for name in jobs}) as read:
            core.search_items("history")
            self.assertEqual(read.call_args.args[1], core.READ_TIMEOUT * 2)
            with patch.dict(os.environ, {"EVEROS_API_VERSION": "v1"}):
                core.search_items("history", method="agentic")
            self.assertEqual(read.call_args.args[1], core.READ_TIMEOUT * 2)

    def test_search_text_forwards_explicit_budget_unchanged(self):
        with patch.object(core, "search_items", return_value=[]) as search:
            core.search_text("history", 100, "practices", method="agentic", strict=True, budget=240.5)
        self.assertEqual(search.call_args.args, ("history", 100, "practices"))
        self.assertEqual(search.call_args.kwargs["budget"], 240.5)
        self.assertEqual(search.call_args.kwargs["method"], "agentic")
        self.assertIs(search.call_args.kwargs["strict"], True)

    def test_search_text_omits_unspecified_budget_to_keep_configuration_default(self):
        with patch.object(core, "search_items", return_value=[]) as search:
            core.search_text("history", method="agentic")
            self.assertNotIn("budget", search.call_args.kwargs)
            search.reset_mock()
            core.search_text("history", method="agentic", budget=None)
            self.assertNotIn("budget", search.call_args.kwargs)

    def test_agentic_reaches_both_v2_tracks_without_hybrid_score_floor(self):
        self.api.side_effect = lambda path, body, timeout: {"data": (
            {"episodes": [self.episode()]} if body.get("user_id") else {
                "agent_cases": [self.case()],
                "agent_skills": [{"id": "skill", "name": "skill", "description": "a useful skill", "score": .01}]})}
        notices = []
        items = core.search_items("implicit historical question", top_k=100, method="agentic", notices=notices)
        self.assertEqual({it["id"] for it in items}, {"episode", "case", "skill"})
        self.assertEqual(self.api.call_count, 2)
        for call in self.api.call_args_list:
            self.assertEqual(call.args[0], "/api/v2/memory/search")
            self.assertEqual(call.args[1]["method"], "agentic")
            self.assertEqual(call.args[1]["top_k"], 100)
            self.assertNotIn("min_score", call.args[1])
        self.assertEqual(notices, [])

    def test_agentic_queries_only_requested_tracks(self):
        for kind, expected in (("episodes", "user_id"), ("practices", "agent_id")):
            with self.subTest(kind=kind):
                self.api.reset_mock()
                self.api.side_effect = lambda path, body, timeout: {"data": (
                    {"episodes": [self.episode()]} if body.get("user_id") else {"agent_cases": [self.case()]})}
                items = core.search_items("history", top_k=-1, kind=kind, method="agentic")
                self.assertEqual(len(items), 1)
                self.api.assert_called_once()
                self.assertIn(expected, self.api.call_args.args[1])
                self.assertEqual(self.api.call_args.args[1]["top_k"], -1)

    def test_invalid_method_fails_before_network_work(self):
        with patch.object(core, "_read_jobs") as read:
            for method in ("keyword", "vector", "Agentic", "", None, False, []):
                with self.subTest(method=method), self.assertRaisesRegex(ValueError, "method"):
                    core.search_items("query", method=method)
            read.assert_not_called()
        self.api.assert_not_called()

    def test_agentic_zero_budget_does_not_start_network_work(self):
        notices = []
        self.assertEqual(core.search_items("history", method="agentic", budget=0, notices=notices), [])
        self.api.assert_not_called()
        self.assertEqual(notices, [])

    def test_user_track_failure_falls_back_without_repeating_successful_agent_track(self):
        def fake_api(path, body, timeout):
            if body.get("agent_id"):
                return {"data": {"agent_cases": [self.case("primary-low-score")]}}
            if body["method"] == "agentic":
                raise TimeoutError("user track unavailable")
            self.assertEqual(body["min_score"], .3)
            return {"data": {"episodes": [self.episode("recovered-user")]}}
        self.api.side_effect = fake_api
        notices = []
        items = core.search_items("history", method="agentic", budget=.3, notices=notices)
        self.assertEqual({it["id"] for it in items}, {"recovered-user", "primary-low-score"})
        self.assertEqual(sum(bool(c.args[1].get("agent_id")) for c in self.api.call_args_list), 1)
        self.assertEqual(sum(c.args[1]["method"] == "hybrid" for c in self.api.call_args_list), 1)
        self.assertEqual(len(notices), 1)
        self.assertIn("agentic", notices[0])
        self.assertIn("hybrid", notices[0])

    def test_empty_agent_track_falls_back_and_restores_hybrid_case_and_skill_floor(self):
        def fake_api(path, body, timeout):
            if body.get("user_id"):
                return {"data": {"episodes": [self.episode("primary-user")]}}
            if body["method"] == "agentic":
                return {"data": {"agent_cases": [], "agent_skills": []}}
            return {"data": {"agent_cases": [self.case("too-low", .01), self.case("at-floor", .55)],
                             "agent_skills": [
                                 {"id": "low-skill", "name": "low", "description": "noise", "score": .01},
                                 {"id": "good-skill", "name": "good", "description": "useful", "score": .8}]}}
        self.api.side_effect = fake_api
        notices = []
        items = core.search_items("history", method="agentic", budget=.3, notices=notices)
        self.assertEqual({it["id"] for it in items}, {"primary-user", "at-floor", "good-skill"})
        self.assertEqual(sum(bool(c.args[1].get("user_id")) for c in self.api.call_args_list), 1)
        self.assertEqual(len(notices), 1)
        self.assertIn("agentic", notices[0])
        self.assertIn("hybrid", notices[0])

    def test_blank_episode_bodies_trigger_fallback_but_readable_subject_is_kept(self):
        for primary in ({"summary": "", "subject": ""}, {"summary": " \n", "subject": "\t "}):
            with self.subTest(primary=primary):
                self.api.reset_mock()
                self.api.side_effect = lambda path, body, timeout: {"data": {"episodes": (
                    [dict(primary, id="blank")] if body["method"] == "agentic" else [self.episode("recovered")])}}
                notices = []
                items = core.search_items("history", kind="episodes", method="agentic", notices=notices)
                self.assertEqual([it["id"] for it in items], ["recovered"])
                self.assertEqual(self.api.call_count, 2)
                self.assertEqual(len(notices), 1)
                self.assertIn("hybrid", notices[0])
        self.api.reset_mock()
        self.api.side_effect = None
        self.api.return_value = {"data": {"episodes": [
            {"id": "subject", "summary": " \n", "subject": " the readable subject "}]}}
        items = core.search_items("history", kind="episodes", method="agentic")
        self.assertEqual([it["id"] for it in items], ["subject"])
        self.assertEqual(items[0]["text"], "the readable subject")
        self.api.assert_called_once()

    def test_hybrid_default_keeps_existing_score_filters_and_never_deepens_empty_results(self):
        self.api.return_value = {"data": {}}
        self.assertEqual(core.search_items("history"), [])
        self.assertEqual(self.api.call_count, 2)
        for call in self.api.call_args_list:
            body = call.args[1]
            self.assertEqual(body["method"], "hybrid")
            if body.get("user_id"):
                self.assertEqual(body["min_score"], .3)
        self.api.reset_mock()
        self.api.return_value = {"data": {"agent_cases": [self.case("low", .01), self.case("high", .6)]}}
        self.assertEqual([it["id"] for it in core.search_items("history", kind="practices")], ["high"])
        self.api.assert_called_once()

    def test_legacy_tracks_remain_hybrid_and_keep_their_score_floor_during_deep_search(self):
        def fake_api(path, body, timeout):
            if path.startswith("/api/v1/"):
                self.assertEqual(body["method"], "hybrid")
                return {"data": {"episodes": [self.episode("legacy-episode")],
                                 "agent_memory": {"cases": [self.case("legacy-low", .02),
                                                             self.case("legacy-high", .8)]}}}
            if body.get("user_id"):
                return {"data": {"episodes": [self.episode("v2-episode", "new direct answer")]}}
            return {"data": {"agent_cases": [self.case("v2-low", .02)]}}
        self.api.side_effect = fake_api
        with patch.dict(os.environ, {"EVEROS_READ_V1": "1"}):
            items = core.search_items("history", method="agentic")
        self.assertEqual({it["id"] for it in items}, {"legacy-episode", "legacy-high", "v2-episode", "v2-low"})
        self.assertEqual(self.api.call_count, 3)

    def test_v1_only_agentic_downgrade_is_visible_and_uses_hybrid(self):
        self.api.return_value = {"data": {"episodes": [self.episode("legacy")]}}
        with patch.dict(os.environ, {"EVEROS_API_VERSION": "v1", "EVEROS_READ_V1": "1"}):
            text = core.search_text("history", top_k=-1, kind="episodes", method="agentic")
        self.assertIn("v1", text)
        self.assertIn("agentic", text)
        self.assertIn("hybrid", text)
        self.assertIn("direct historical answer", text)
        self.api.assert_called_once()
        self.assertEqual(self.api.call_args.args[1]["method"], "hybrid")
        self.assertEqual(self.api.call_args.args[1]["top_k"], -1)

    def test_empty_primary_and_empty_fallback_are_successful_empty_search(self):
        self.api.return_value = {"data": {}}
        notices = []
        self.assertEqual(core.search_items("history", method="agentic", budget=.3, notices=notices), [])
        self.assertEqual(self.api.call_count, 4)
        self.assertEqual(len(notices), 2)
        self.assertTrue(all("agentic" in note and "hybrid" in note for note in notices))

    def test_every_track_and_fallback_technical_failure_is_reported(self):
        self.api.side_effect = TimeoutError("offline")
        with self.assertRaises(core.APIResponseError):
            core.search_items("history", method="agentic", budget=.3)
        self.assertEqual(self.api.call_count, 4)
        self.assertEqual({c.args[1]["method"] for c in self.api.call_args_list}, {"agentic", "hybrid"})

    def test_failed_fallback_keeps_successful_track_and_exposes_degradation(self):
        def fake_api(path, body, timeout):
            if body.get("user_id"):
                return {"data": {"episodes": [self.episode("kept")]}}
            raise RuntimeError("practice service down")
        self.api.side_effect = fake_api
        text = core.search_text("history", method="agentic")
        self.assertIn("direct historical answer", text)
        self.assertIn("agentic", text)
        self.assertIn("hybrid", text)
        self.assertEqual(sum(bool(c.args[1].get("user_id")) for c in self.api.call_args_list), 1)

    def test_default_agentic_budget_reserves_time_for_fallback(self):
        budgets = []
        def empty_tracks(jobs, budget):
            budgets.append(budget)
            return {name: {} for name in jobs}
        with patch.object(core, "_read_jobs", side_effect=empty_tracks):
            core.search_items("history", method="agentic")
        self.assertEqual(len(budgets), 2)
        self.assertGreater(budgets[0], 167)
        self.assertLessEqual(budgets[0], 168)
        self.assertGreater(budgets[1], 11)
        self.assertLessEqual(budgets[1], 12)

    def test_primary_and_fallback_share_one_deadline_and_keep_fast_results(self):
        def fake_api(path, body, timeout):
            if body.get("agent_id"):
                return {"data": {"agent_cases": [self.case("fast")]}}
            time.sleep(.8)
            return {"data": {"episodes": [self.episode("late")]}}
        self.api.side_effect = fake_api
        notices = []
        start = time.monotonic()
        items = core.search_items("history", method="agentic", budget=.12, notices=notices)
        self.assertLess(time.monotonic() - start, .45)
        self.assertEqual([it["id"] for it in items], ["fast"])
        self.assertEqual(sum(bool(c.args[1].get("agent_id")) for c in self.api.call_args_list), 1)
        self.assertTrue(all(0 < c.kwargs["timeout"] <= .12 for c in self.api.call_args_list))
        self.assertTrue(any("hybrid" in note for note in notices))

    def test_mcp_search_output_includes_fallback_notice_within_existing_character_budget(self):
        def fake_api(path, body, timeout):
            if body.get("user_id") and body["method"] == "agentic":
                return {"data": {}}
            if body.get("user_id"):
                return {"data": {"episodes": [self.episode(str(i), f"answer {i}: " + "x" * 400)
                                               for i in range(100)]}}
            return {"data": {"agent_cases": [self.case("practice")]}}
        self.api.side_effect = fake_api
        text = core.search_text("history", top_k=100, method="agentic")
        self.assertLessEqual(len(text), 4000)
        self.assertIn("agentic", text)
        self.assertIn("hybrid", text)
        self.assertIn("answer 0:", text)

    def test_hook_remains_hybrid_with_its_existing_eighteen_second_budget(self):
        with patch.dict(os.environ, {"EVEROS_RECALL_TIMEOUT": "18"}), \
                patch.object(core, "search_items", return_value=[]) as search:
            hook.on_prompt({"session_id": "hook-still-fast", "prompt": "meaningful question"})
        self.assertEqual(search.call_args.kwargs.get("method", "hybrid"), "hybrid")
        self.assertGreater(search.call_args.kwargs["budget"], 17)
        self.assertLessEqual(search.call_args.kwargs["budget"], 18)

    def test_deep_search_subprocess_exits_without_waiting_for_late_workers(self):
        script = "\n".join([
            "import sys, time", f"sys.path.insert(0, {str(SOURCE)!r})", "import core",
            "def slow(*args, **kwargs):", "    time.sleep(3)", "    return {'data': {}}",
            "core.api = slow", "try:",
            "    core.search_items('history', method='agentic', budget=.1)",
            "except core.APIResponseError:", "    print('failed within budget')", "print('finished')",
        ])
        start = time.monotonic()
        out = subprocess.run([sys.executable, "-c", script], env=dict(os.environ), capture_output=True,
                             text=True, timeout=2)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("failed within budget", out.stdout)
        self.assertIn("finished", out.stdout)
        self.assertLess(time.monotonic() - start, 1.5)


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


class FakeClock:
    """单调时钟随 sleep 前进，回读轮询不必真的等待。"""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PracticeTests(IsolatedCore):
    STEPS = [{"action": "act 1", "why": "why 1", "result": "result 1"},
             {"action": "act 2", "result": "result 2", "outcome": "failed"},
             {"action": "act 3", "result": "result 3", "outcome": "unexpected", "user_correction": "use X instead"}]

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.stack.enter_context(patch.object(core.time, "monotonic", self.clock.monotonic))
        self.stack.enter_context(patch.object(core.time, "sleep", self.clock.sleep))
        self.polls = []
        self.api.side_effect = self.fake_api

    def fake_api(self, path, body, timeout=90):
        if path == "/api/v2/memory/add":
            return response("extracted")
        if path in ("/api/v2/memory/flush", "/api/v1/memories/agent/flush"):
            return response("no_extraction" if path.startswith("/api/v2") else "extracted")
        if path == "/api/v1/memories/agent":
            return response("accumulated")
        if path == "/api/v2/memory/get":
            item = self.polls.pop(0) if self.polls else []
            if isinstance(item, Exception):
                raise item
            return {"data": {"agent_cases": item(body) if callable(item) else item}}
        raise AssertionError(f"unexpected API path {path}")

    def paths(self):
        return [c.args[0] for c in self.api.call_args_list]

    @staticmethod
    def case_for(body, timestamp=None, quality=0.9):
        stamp = timestamp or datetime_utc_now()
        return [{"session_id": body["filters"]["session_id"], "timestamp": stamp, "quality_score": quality}]

    def test_practice_requires_three_real_steps_before_any_write(self):
        ok = {"action": "act", "result": "done"}
        invalid = [
            ([], "conclusion", "至少需要 3 个真实步骤"), ([ok, ok], "conclusion", "memory_remember"),
            ([ok, ok, "step"], "conclusion", "必须是对象"), ([ok, ok, {"action": "act"}], "conclusion", "缺少"),
            ([ok, ok, {"action": " ", "result": "x"}], "conclusion", "缺少"),
            ([ok, ok, dict(ok, outcome="maybe")], "conclusion", "outcome"),
            ([ok, ok, dict(ok, why=[])], "conclusion", "why"),
            ([ok, ok, dict(ok, user_correction=1)], "conclusion", "user_correction"),
            ([ok, ok, ok], " ", "conclusion"),
        ]
        for steps, conclusion, message in invalid:
            with self.subTest(steps=steps, conclusion=conclusion):
                with self.assertRaisesRegex(ValueError, message):
                    core.record_practice("codex", "topic", "task", steps, conclusion)
        self.api.assert_not_called()

    def test_practice_messages_surface_failures_and_user_corrections(self):
        msgs = core.practice_messages("codex", "task", self.STEPS, "conclusion", now_ms=10_000)
        self.assertEqual([m["role"] for m in msgs], [
            "user", "assistant", "tool", "assistant", "tool", "assistant",
            "assistant", "tool", "assistant", "user", "assistant"])
        self.assertEqual(msgs[1]["content"], "act 1\n原因：why 1")
        self.assertEqual(msgs[2]["content"], "result 1")
        self.assertEqual(msgs[4]["content"], "【失败】result 2")
        self.assertEqual(msgs[5]["content"], "这一步没走通：result 2")
        self.assertEqual(msgs[7]["content"], "【意外】result 3")
        self.assertEqual(msgs[8]["content"], "这一步的结果出乎意料：result 3")
        self.assertEqual((msgs[9]["sender_id"], msgs[9]["content"]), ("test_owner", "use X instead"))
        self.assertEqual(msgs[-1]["content"], "conclusion")
        self.assertEqual(sum(1 for m in msgs if m.get("tool_calls")), 3)
        self.assertEqual([m["tool_call_id"] for m in msgs if m["role"] == "tool"], ["call_1", "call_2", "call_3"])
        stamps = [m["timestamp"] for m in msgs]
        self.assertEqual(stamps, sorted(set(stamps)))
        self.assertEqual(stamps[-1], 9_999)
        mapped = core._v2_messages(msgs)
        self.assertEqual({m["sender_id"] for m in mapped if m["role"] == "user"}, {"test_owner"})
        self.assertEqual({m["sender_id"] for m in mapped if m["role"] != "user"}, {"test_owner_agent"})

    def test_reflection_keeps_long_results_visible_but_bounded(self):
        long_result = "发现：" + "x" * 1000
        msgs = core.practice_messages("codex", "task", [dict(self.STEPS[1], result=long_result)] * 3, "done")
        reflection = msgs[3]["content"]
        self.assertTrue(reflection.startswith("这一步没走通：发现："))
        self.assertLess(len(reflection), 500)
        self.assertEqual(msgs[2]["content"], "【失败】" + long_result)

    def test_readback_reports_generated_case_after_polling(self):
        self.polls = [[], lambda body: self.case_for(body)]
        out = core.record_practice("codex", "topic", "task", self.STEPS, "conclusion")
        self.assertIn("v2 extracted/no_extraction；v2 做法 case 已生成（质量 0.9", out)
        self.assertEqual(self.paths(), ["/api/v2/memory/add", "/api/v2/memory/flush",
                                        "/api/v2/memory/get", "/api/v2/memory/get"])
        body = self.api.call_args_list[2].args[1]
        self.assertEqual((body["memory_type"], body["agent_id"]), ("agent_case", "test_owner_agent"))
        self.assertTrue(body["filters"]["session_id"].startswith("agent_") and body["filters"]["session_id"].endswith("_topic"))
        self.assertIn("practice case agent_", core.LOG.read_text())
        self.assertIn("found", core.LOG.read_text())

    def test_readback_ignores_stale_or_foreign_cases_and_reports_missing(self):
        stale = lambda body: self.case_for(body, timestamp="2026-01-01T00:00:00Z")
        foreign = [{"session_id": "agent_other", "timestamp": datetime_utc_now(), "quality_score": 1}]
        self.polls = [stale, foreign] * 20
        with patch.dict(os.environ, {"EVEROS_CASE_WAIT": "9"}):
            out = core.record_practice("codex", "topic", "task", self.STEPS, "conclusion")
        self.assertIn("v2 做法 case 在 9 秒内没有生成", out)
        self.assertIn("memory_search(kind=practices)", out)
        self.assertEqual(self.paths().count("/api/v2/memory/get"), 4)  # 回读 0/3/6/9 秒，到上限即停
        self.assertIn("missing after 9s", core.LOG.read_text())

    def test_readback_failure_never_turns_a_written_practice_into_an_error(self):
        self.polls = [RuntimeError("get down")] * 40
        out = core.record_practice("codex", "topic", "task", self.STEPS, "conclusion")
        self.assertIn("v2 做法 case 回读失败（get down）；写入本身已完成", out)
        self.assertIn("check failed", core.LOG.read_text())

    def test_disabled_wait_and_v1_only_skip_v2_case_readback(self):
        with patch.dict(os.environ, {"EVEROS_CASE_WAIT": "0"}):
            out = core.record_practice("codex", "topic", "task", self.STEPS, "conclusion")
        self.assertTrue(out.endswith("v2 做法 case 未回读（EVEROS_CASE_WAIT=0）"))
        self.api.reset_mock()
        with patch.dict(os.environ, {"EVEROS_API_VERSION": "v1"}):
            out = core.record_practice("codex", "topic", "task", self.STEPS, "conclusion")
        self.assertEqual(self.paths(), ["/api/v1/memories/agent", "/api/v1/memories/agent/flush"])
        self.assertNotIn("做法 case", out)

    def test_dual_write_sends_one_trajectory_to_both_versions_before_checking_v2(self):
        self.polls = [lambda body: self.case_for(body)]
        with patch.dict(os.environ, {"EVEROS_READ_V1": "1"}):
            out = core.record_practice("codex", "topic", "task", self.STEPS, "conclusion")
        self.assertEqual(self.paths(), ["/api/v2/memory/add", "/api/v2/memory/flush", "/api/v1/memories/agent",
                                        "/api/v1/memories/agent/flush", "/api/v2/memory/get"])
        v2 = self.api.call_args_list[0].args[1]["messages"]
        v1 = self.api.call_args_list[2].args[1]["messages"]
        self.assertEqual([(m["role"], m["content"], m["timestamp"]) for m in v2],
                         [(m["role"], m["content"], m["timestamp"]) for m in v1])
        self.assertIn("v2 extracted/no_extraction; v1 accumulated/extracted；v2 做法 case 已生成", out)

    def test_case_wait_config_and_timestamp_parsing(self):
        self.assertEqual(core.case_wait(), 30)
        for value, expected in (("0", 0), ("12.5", 12.5), ("abc", 30), ("-1", 30), ("nan", 30), ("1e9", 150)):
            with self.subTest(value=value), patch.dict(os.environ, {"EVEROS_CASE_WAIT": value}):
                self.assertEqual(core.case_wait(), expected)
        stamp = 1792133910428
        for value, expected in (("2026-10-16T06:58:30.428000Z", stamp), ("2026-10-16T06:58:30.428Z", stamp),
                                ("2026-10-16T06:58:30.42Z", stamp - 8), ("2026-10-16T06:58:30.428000999+00:00", stamp),
                                ("2026-10-16T14:58:30.428+08:00", stamp), ("2026-10-16T06:58:30", stamp - 428),
                                (stamp, stamp), (stamp / 1000, stamp), (str(stamp), stamp),
                                (None, 0), ("", 0), ("garbage", 0), (True, 0)):
            with self.subTest(value=value):
                self.assertEqual(core.epoch_ms(value), expected)


def datetime_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 5))


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
