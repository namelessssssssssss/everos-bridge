"""Observable retrieval, expansion, context recovery, and hook behavior with isolated fake memories."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_core_regressions import IsolatedCore, core, hook
import mcp_server
import recall


class RetrievalWorkflowTests(IsolatedCore):
    def item(self, tail="Do not repeat the old migration."):
        return recall.snapshot({"id": "row", "kind": "episode", "text": "summary"}, {
            "id": "row", "summary": "summary", "episode": "long details " * 60 + tail,
            "timestamp": "2026-01-01", "embedding": [99], "api_key": "must not be copied"}, "v2/episode")

    def test_snapshot_expands_hidden_details_with_explicit_pagination(self):
        item = self.item()
        first = json.loads(recall.open_memory(item["ref"], limit=260))
        second = json.loads(recall.open_memory(item["ref"], offset=first["next_offset"], limit=12000))
        self.assertTrue(first["truncated"])
        self.assertIn("Do not repeat", second["text"])
        self.assertIsNone(second["next_offset"])
        self.assertNotIn("must not be copied", first["text"] + second["text"])
        self.assertEqual(first["source"], "v2/episode")
        self.assertEqual((core.STATE / "reads" / f"{item['ref']}.json").stat().st_mode & 0o777, 0o600)
        self.api.assert_not_called()

    def test_handles_cannot_read_paths_or_cross_identity_boundaries(self):
        ref = self.item()["ref"]
        with self.assertRaises(ValueError):
            recall.open_memory("../../config.env")
        with patch.dict(os.environ, {"EVEROS_USER_ID": "different-owner"}):
            with self.assertRaisesRegex(ValueError, "身份不匹配"):
                recall.open_memory(ref)
        with self.assertRaises(ValueError):
            recall.open_memory(ref, offset=999999)

    def test_snapshot_content_versions_survive_restarts_and_differ_on_change(self):
        first = self.item()["ref"]
        self.assertEqual(self.item()["ref"], first)
        self.assertNotEqual(self.item("changed decision")["ref"], first)
        # The handle reads from disk rather than an in-memory server map.
        self.assertIn("Do not repeat", recall.open_memory(first))

    def test_search_reports_expandable_id_date_source_and_truncation(self):
        self.api.side_effect = lambda path, body, timeout: {"data": {"episodes": [{
            "id": "episode", "summary": "short finding", "episode": "details " * 70,
            "timestamp": "2026-01-01"}]}}
        text = core.search_text("question", kind="episodes", method="agentic")
        self.assertIn("id=em_", text)
        self.assertIn("source=v2/episode", text)
        self.assertIn("truncated=true", text)
        self.assertIn("2026-01-01", text)

    def test_same_question_reuses_but_new_question_and_new_version_are_shown(self):
        item = self.item()
        with patch.object(core, "search_items", return_value=[item]):
            self.assertTrue(any("summary" in b for b in hook.build_prompt_blocks("context", "question A")))
            self.assertFalse(any("summary" in b for b in hook.build_prompt_blocks("context", "question A")))
            self.assertTrue(any("summary" in b for b in hook.build_prompt_blocks("context", "question B")))
        with patch.object(core, "search_items", return_value=[self.item("changed")]):
            self.assertTrue(any("summary" in b for b in hook.build_prompt_blocks("context", "question A")))

    def test_context_keeps_new_clues_in_query_even_with_long_goal(self):
        recall.update_context("context", goal="goal " * 100, stage="new_evidence", clues="E_FIXTURE_42")
        with patch.object(core, "search_items", return_value=[]) as search:
            core.search_text("What happened?", session_id="context", method="agentic")
        self.assertIn("E_FIXTURE_42", search.call_args.args[0])
        self.assertLessEqual(len(search.call_args.args[0]), 500)

    def test_recovery_and_goal_change_invalidate_only_local_coverage(self):
        with recall.locked("context") as state:
            state.update(goal="old", coverage={"question": {"refs": ["old"]}}, ids=["old"])
        data = json.loads(recall.update_context("context", stage="recover"))
        self.assertEqual(data["covered_questions"], [])
        self.assertEqual(data["generation"], 1)
        recall.update_context("context", goal="new")
        self.assertEqual(core.load_state(recall.state_path("context"))["generation"], 2)
        self.api.assert_not_called()

    def test_compaction_invalidates_coverage_even_when_writes_are_disabled(self):
        with recall.locked("compact") as state:
            state.update(coverage={"question": {}}, ids=["old"])
        payload = json.dumps({"session_id": "compact", "hook_event_name": "PreCompact"})
        with patch.dict(os.environ, {"EVEROS_WRITE": "0"}), patch.object(hook.sys, "argv", ["hook.py"]), \
                patch.object(hook.sys, "stdin", io.StringIO(payload)):
            self.assertEqual(hook.main(), 0)
        self.assertEqual(core.load_state(recall.state_path("compact"))["coverage"], {})

    def test_recovery_refreshes_profile_on_next_prompt_for_kimi_and_mimo_too(self):
        for host in ("kimi", "kimi_desktop", "mimocode"):
            recall.update_context(host, stage="recover")
            with patch.object(hook, "HOST_OVERRIDE", host), \
                    patch.object(core, "search_items", return_value=[]), \
                    patch.object(core, "profile_text", return_value="refreshed profile") as profile:
                blocks = hook.build_prompt_blocks(host, "continue")
            self.assertEqual(profile.call_count, 1)
            self.assertTrue(any("refreshed profile" in block for block in blocks))

    def test_tool_reminders_are_local_bounded_and_do_not_recurse(self):
        payload = {"session_id": "tools", "hook_event_name": "PreToolUse", "tool_name": "apply_patch",
                   "tool_input": {"command": "update component"}}
        self.assertIn("历史决策", recall.tool_reminder(payload))
        self.assertEqual(recall.tool_reminder(payload), "")
        payload.update(hook_event_name="PostToolUse", tool_name="Bash", tool_response={"output": "x" * 1000 + " error E_FIXTURE_42"})
        self.assertIn("失败", recall.tool_reminder(payload))
        self.assertIn("E_FIXTURE_42", core.load_state(recall.state_path("tools"))["clues"])
        payload["tool_name"] = "mcp__everos__memory_search"
        self.assertEqual(recall.tool_reminder(payload), "")
        self.api.assert_not_called()

    def test_mcp_expansion_and_context_validation_work_across_server_instances(self):
        ref = self.item()["ref"]
        result = mcp_server.Server().call("memory_open", {"memory_id": ref, "offset": 260})
        self.assertIn("Do not repeat", result)
        data = json.loads(mcp_server.Server().call("memory_context", {
            "session_id": "context", "goal": "task", "stage": "new_evidence", "clues": "E_FIXTURE_42"}))
        self.assertIn("E_FIXTURE_42", data["context"])
        self.assertEqual(recall.state_path("context").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(mcp_server.RpcError):
            mcp_server.Server().call("memory_open", {"memory_id": ref, "offset": -1})
        with self.assertRaises(mcp_server.RpcError):
            mcp_server.Server().call("memory_context", {"session_id": "context", "clues": "x" * 501})

    def test_real_hook_process_emits_only_a_local_reminder_and_honors_inject_switch(self):
        payload = {"session_id": "process", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                   "tool_response": {"output": "error E_FIXTURE_42"}}
        command = [sys.executable, str(Path(hook.__file__).resolve())]
        env = dict(os.environ, EVEROS_API_BASE="http://127.0.0.1:9", EVEROS_INJECT="1")
        first = subprocess.run(command, input=json.dumps(payload), env=env, text=True,
                               capture_output=True, timeout=2)
        result = json.loads(first.stdout)
        self.assertIn("任务中检索提醒", result["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", result["hookSpecificOutput"])
        payload["session_id"] = "disabled"
        second = subprocess.run(command, input=json.dumps(payload), env=dict(env, EVEROS_INJECT="0"),
                                text=True, capture_output=True, timeout=2)
        self.assertEqual(second.stdout, "")
        self.assertFalse(recall.state_path("disabled").exists())
