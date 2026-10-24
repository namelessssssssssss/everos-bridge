"""Doubao Work trajectory parsing and incremental capture: offline regressions only.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "plugin" / "everos"
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "doubao"))
import core
import doubao


def row(role, content, tool_calls=None):
    r = {"role": role, "content": content}
    if tool_calls is not None:
        r["tool_calls"] = tool_calls
    return r


ASSIGN = """# Assignment
- Agent ID: main-agent
- Role: main

> append-only

## [2026-10-16T11:15:36.937Z] 需求
first turn
## [2026-10-16T11:20:00.000Z] 需求
second turn
## [2026-10-16T11:30:00.000Z] 需求
third turn
"""


class DoubaoParseTests(unittest.TestCase):
    def setUp(self):
        # messages_doubao() stamps the owner id; parsing must not require a configured EverOS.
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(core, "owner", return_value="test_owner"))

    def test_only_user_text_and_final_answers_are_kept(self):
        rows = [
            row("user", "first turn"),
            row("assistant", "进度旁白", tool_calls=[{"id": "t1"}]),
            {"role": "tool", "content": "tool output", "tool_call_id": "t1"},
            row("assistant", "最终回复"),
            row("user", "<system-reminder>系统提醒</system-reminder>\nsecond turn"),
        ]
        msgs = doubao.messages_doubao(rows, [1_792_149_336_000, 1_792_149_600_000], 9_999_999_999_999)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[0]["content"], "first turn")
        self.assertEqual(msgs[1]["content"], "最终回复")
        self.assertNotIn("system-reminder", msgs[2]["content"])
        self.assertEqual(msgs[2]["content"], "second turn")
        # final answer sits one ms before the following turn
        self.assertEqual(msgs[1]["timestamp"], 1_792_149_599_999)
        self.assertEqual(msgs[0]["sender_id"], msgs[2]["sender_id"])
        self.assertEqual(msgs[1]["sender_id"], doubao.HOST)

    def test_duplicate_user_rows_collapse_and_monotonic_timestamps(self):
        rows = [row("user", "same"), row("user", "same"),
                row("assistant", "tool narration", tool_calls=[{"id": "x"}]),
                row("assistant", "more narration", tool_calls=[{"id": "y"}])]
        msgs = doubao.messages_doubao(rows, [1000, 2000], 5000)
        self.assertEqual([m["content"] for m in msgs], ["same"])
        ts = [m["timestamp"] for m in msgs]
        self.assertEqual(ts, sorted(ts))

    def test_structured_content_parts(self):
        rows = [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "b"}]}]
        msgs = doubao.messages_doubao(rows, [1000], 2000)
        self.assertEqual([m["content"] for m in msgs], ["a", "b"])

    def test_trailing_final_uses_fallback_timestamp(self):
        rows = [row("user", "q"), row("assistant", "last answer")]
        msgs = doubao.messages_doubao(rows, [1000], 7777)
        self.assertEqual(msgs[1]["timestamp"], 7777)

    def test_session_name_has_dw_prefix_and_day(self):
        name = doubao.session_name("38447516277377794", [row("user", "接入 EverOS 记忆")], [1_792_149_336_000])
        self.assertTrue(name.startswith("dw_20261016_"))
        self.assertTrue(name.endswith("38447516277377794"))

    def test_assignment_timestamps_parse_all_turns(self):
        self.assertEqual(len(doubao.assignment_timestamps(ASSIGN)), 3)


class BinEntryTests(unittest.TestCase):
    def test_bin_wrappers_can_import_core(self):
        """Bin wrappers must add plugin/everos to sys.path (doubao.py imports core)."""
        root = Path(__file__).resolve().parents[1]
        for entry in ("bin/everos-doubao-ingest", "bin/everos-mcp"):
            code = (root / entry).read_text()
            self.assertIn('plugin" / "everos"', code, entry)
            self.assertTrue((root / entry).stat().st_mode & 0o111, entry)

    def test_bin_help_runs_without_config(self):
        import subprocess
        root = Path(__file__).resolve().parents[1]
        for entry in ("bin/everos-doubao-ingest", "bin/everos-mcp"):
            result = subprocess.run([sys.executable, str(root / entry), "--help"], capture_output=True, text=True,
                                    env={**os.environ, "EVEROS_HOME": "/nonexistent"}, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage", result.stdout.lower())


class McpCallTimeoutTests(unittest.TestCase):
    def setUp(self):
        import mcp_call
        self.m = mcp_call

    def test_agentic_search_defaults_to_600s_budget_and_longer_wait(self):
        for args in ({"query": "q"}, {"query": "q", "method": "agentic"}):
            out, wait = self.m.prepare("memory_search", args, env={})
            self.assertEqual(out["timeout"], 600.0)
            self.assertEqual(wait, 630.0)
            self.assertNotIn("timeout", args)

    def test_explicit_timeout_and_hybrid_are_untouched(self):
        out, wait = self.m.prepare("memory_search", {"query": "q", "timeout": 60}, env={})
        self.assertEqual((out["timeout"], wait), (60, 200.0))
        out, wait = self.m.prepare("memory_search", {"query": "q", "method": "hybrid"}, env={})
        self.assertNotIn("timeout", out)
        self.assertEqual(wait, 200.0)
        out, wait = self.m.prepare("memory_profile", {}, env={})
        self.assertEqual((out, wait), ({}, 200.0))

    def test_env_overrides_budget_and_client_wait(self):
        out, wait = self.m.prepare("memory_search", {"query": "q"}, env={"EVEROS_DOUBAO_AGENTIC_TIMEOUT": "300"})
        self.assertEqual((out["timeout"], wait), (300.0, 330.0))
        _, wait = self.m.prepare("memory_search", {"query": "q"}, env={"EVEROS_MCP_CLIENT_TIMEOUT": "900"})
        self.assertEqual(wait, 900.0)

    def test_invalid_env_is_rejected(self):
        for bad in ("0", "-1", "nan", "inf", "abc"):
            with self.assertRaises(ValueError):
                self.m.prepare("memory_search", {"query": "q"}, env={"EVEROS_DOUBAO_AGENTIC_TIMEOUT": bad})


class DoubaoSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "sessions"
        sid = "38447504981617922"
        agent = self.sessions / sid / "agents" / "m_agent" / "system"
        agent.mkdir(parents=True)
        (agent / "assignment.md").write_text(
            "- Role: main\n\n## [2026-10-16T12:00:00.000Z] 需求\nq\n", encoding="utf-8")
        self.traj = agent / "trajectory.jsonl"
        self.sid = sid
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "EVEROS_HOME": str(self.root / "everos"), "EVEROS_USER_ID": "test_owner",
            "EVEROS_API_KEY": "k", "EVEROS_API_VERSION": "v2", "EVEROS_WRITE": "1"}))
        for name, path in {
            "HOME": self.root, "STATE": self.root / "everos/state",
            "SESS_DIR": self.root / "everos/state/sessions", "RECALL_DIR": self.root / "everos/state/recall",
            "LOG": self.root / "everos/state/hook.log", "SWEEP_MARK": self.root / "everos/state/sweep",
            "LOCALMEM_ROOT": self.root / "projects", "LOCALMEM_LEDGER": self.root / "everos/state/localmem.json"}.items():
            self.stack.enter_context(patch.object(core, name, path))
        self.adds = []
        self.stack.enter_context(patch.object(core, "post_messages", side_effect=self._add))
        self.stack.enter_context(patch.object(core, "flush", return_value="no_extraction"))
        self.stack.enter_context(patch.object(core, "sweep"))

    def _add(self, sid, msgs, **kw):
        self.adds.append((sid, [dict(m) for m in msgs]))
        return "extracted"

    def write_traj(self, rows):
        self.traj.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def ledger(self):
        return core.load_state(core.SESS_DIR / f"doubao_{self.sid}.json")

    def test_dry_run_does_not_write(self):
        self.write_traj([row("user", "q"), row("assistant", "a")])
        out = doubao.ingest_session(self.sid, sessions_root=self.sessions, dry_run=True)
        self.assertEqual(out["new_messages"], 2)
        self.assertEqual(self.adds, [])
        self.assertFalse((core.SESS_DIR / f"doubao_{self.sid}.json").exists())

    def test_incremental_capture_is_idempotent(self):
        self.write_traj([row("user", "q"), row("assistant", "first answer")])
        out1 = doubao.ingest_session(self.sid, sessions_root=self.sessions)
        self.assertEqual(out1["new_messages"], 2)
        out2 = doubao.ingest_session(self.sid, sessions_root=self.sessions)
        self.assertEqual(out2["new_messages"], 0)
        self.assertEqual(len(self.adds), 1)  # second add never happens

    def test_next_turn_captures_previous_final_only_once(self):
        self.write_traj([row("user", "q1")])
        doubao.ingest_session(self.sid, sessions_root=self.sessions)
        # final answer for turn 1 arrives, then turn 2 starts
        self.write_traj([row("user", "q1"), row("assistant", "a1"), row("user", "q2")])
        out = doubao.ingest_session(self.sid, sessions_root=self.sessions)
        roles = [m["role"] for _, ms in self.adds[1:2] for m in ms]
        self.assertEqual(roles, ["assistant", "user"])
        self.assertGreater(out["offset_after"], 0)
        # rerun: nothing new
        self.assertEqual(doubao.ingest_session(self.sid, sessions_root=self.sessions)["new_messages"], 0)
        all_roles = [m["role"] for _, ms in self.adds for m in ms]
        self.assertEqual(all_roles.count("assistant"), 1)

    def test_add_failure_does_not_advance_offset(self):
        self.write_traj([row("user", "q1")])
        with patch.object(core, "post_messages", side_effect=core.APIResponseError("boom")):
            with self.assertRaises(core.APIResponseError):
                doubao.ingest_session(self.sid, sessions_root=self.sessions)
        self.assertEqual(self.ledger().get("offset", 0), 0)
        # retry after recovery works
        doubao.ingest_session(self.sid, sessions_root=self.sessions)
        self.assertGreater(self.ledger()["offset"], 0)

    def test_rewritten_trajectory_stops_instead_of_replaying(self):
        self.write_traj([row("user", "original")])
        doubao.ingest_session(self.sid, sessions_root=self.sessions)
        self.write_traj([row("user", "rewritten head")])
        with self.assertRaisesRegex(RuntimeError, "已被改写"):
            doubao.ingest_session(self.sid, sessions_root=self.sessions)

    def test_half_written_last_line_is_left_for_next_run(self):
        self.traj.write_text(json.dumps(row("user", "q1"), ensure_ascii=False) + "\n"
                            + json.dumps(row("user", "q2"), ensure_ascii=False)[:-3], encoding="utf-8")
        out = doubao.ingest_session(self.sid, sessions_root=self.sessions)
        self.assertEqual([m["content"] for _, ms in self.adds for m in ms], ["q1"])
        self.traj.write_text(json.dumps(row("user", "q1"), ensure_ascii=False) + "\n"
                            + json.dumps(row("user", "q2"), ensure_ascii=False) + "\n", encoding="utf-8")
        doubao.ingest_session(self.sid, sessions_root=self.sessions)
        captured = [m["content"] for _, ms in self.adds for m in ms]
        self.assertEqual(captured, ["q1", "q2"])

    def test_main_agent_dir_ignores_non_main_agents(self):
        other = self.sessions / self.sid / "agents" / "m_other" / "system"
        other.mkdir(parents=True)
        (other / "assignment.md").write_text("- Role: organizer\n", encoding="utf-8")
        found = doubao.main_agent_dir(self.sessions, self.sid)
        self.assertEqual(found.name, "m_agent")

    def test_flush_flag_is_passed_through(self):
        self.write_traj([row("user", "q1")])
        out = doubao.ingest_session(self.sid, sessions_root=self.sessions, flush=True)
        self.assertEqual(out.get("flush"), "no_extraction")
        self.assertFalse(self.ledger().get("pending"))

    def test_invalid_session_id_rejected(self):
        with self.assertRaises(ValueError):
            doubao.ingest_session("../etc", sessions_root=self.sessions)


if __name__ == "__main__":
    unittest.main()
