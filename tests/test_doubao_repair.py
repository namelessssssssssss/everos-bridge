"""Synthetic regressions for Doubao capture and legacy migration."""
import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugin/everos"), str(ROOT / "plugin/doubao")]
import core
import doubao


class CaptureRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sid = "123456789"
        self.system = self.root / "sessions" / self.sid / "agents/m_test/system"
        self.system.mkdir(parents=True)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"EVEROS_USER_ID": "synthetic_owner", "EVEROS_API_KEY": "synthetic", "EVEROS_WRITE": "1"}))
        for name, path in {"STATE": self.root / "state", "SESS_DIR": self.root / "state/sessions", "LOG": self.root / "log"}.items():
            self.stack.enter_context(patch.object(core, name, path))
        self.add = self.stack.enter_context(patch.object(core, "post_messages", return_value="extracted"))
        self.flush = self.stack.enter_context(patch.object(core, "flush", return_value="no_extraction"))
        self.sweep = self.stack.enter_context(patch.object(core, "sweep"))
        self.assign = "- Role: main\n## [2026-10-16T11:00:00Z] 需求\nfirst\n## [2026-10-16T12:00:00Z] 需求\n\n## [2026-11-01T10:00:00Z] 需求\nsecond\n"
        self.write([{"role": "user", "content": "first"}, {"role": "assistant", "content": "answer"}, {"role": "user", "content": "second"}])

    def write(self, rows):
        (self.system / "assignment.md").write_text(self.assign)
        (self.system / "trajectory.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def run_capture(self, **kwargs):
        return doubao.ingest_session(self.sid, sessions_root=self.root / "sessions", **kwargs)

    def test_empty_assignment_does_not_shift_time(self):
        result = self.run_capture(dry_run=True)
        self.assertEqual(result["preview"][-1]["timestamp"], doubao.parse_ts("2026-11-01T10:00:00Z"))
        self.assertEqual(result["preview"][1]["timestamp"], doubao.parse_ts("2026-11-01T10:00:00Z") - 1)

    def test_legacy_row_offset_is_not_treated_as_bytes(self):
        core.SESS_DIR.mkdir(parents=True)
        data = doubao.read_session(self.root / "sessions", self.sid)
        ledger = core.SESS_DIR / f"doubao_{self.sid}.json"
        original = {"host": "doubao_work", "offset": 2, "head_hash": data["head_hash"], "everos_session_id": "synthetic-session"}
        ledger.write_text(json.dumps(original))
        result = self.run_capture(dry_run=True)
        self.assertEqual([p["head"] for p in result["preview"]], ["second"])
        self.assertEqual(json.loads(ledger.read_text()), original)
        self.run_capture()
        migrated = json.loads(ledger.read_text())
        self.assertEqual(migrated["offset_unit"], "bytes-v1")
        self.assertEqual(migrated["offset"], data["length"])
        self.assertTrue(list(core.SESS_DIR.glob("*.bak-offset-*")))

    def test_main_role_fallback_is_unique_and_rejects_ambiguous(self):
        (self.system / "assignment.md").write_text("")
        self.assertEqual(doubao.main_agent_dir(self.root / "sessions", self.sid), self.system.parent)
        other = self.system.parents[1] / "m_other/system"
        other.mkdir(parents=True)
        (other / "trajectory.jsonl").write_text('{}\n')
        with self.assertRaisesRegex((ValueError, FileNotFoundError), "ambiguous|多个|不唯一"):
            doubao.main_agent_dir(self.root / "sessions", self.sid)

    def test_dry_run_does_not_create_state_or_lock(self):
        self.run_capture(dry_run=True)
        self.assertFalse(core.STATE.exists())

    def test_session_ingest_does_not_sweep_other_sessions(self):
        self.run_capture()
        self.sweep.assert_not_called()

    def test_unknown_add_status_does_not_advance(self):
        self.add.return_value = "queued"
        with self.assertRaises(core.APIResponseError):
            self.run_capture()
        self.assertFalse((core.SESS_DIR / f"doubao_{self.sid}.json").exists())

    def test_unknown_flush_status_keeps_pending(self):
        self.flush.return_value = "queued"
        with self.assertRaises(core.APIResponseError):
            self.run_capture(flush=True)
        st = core.load_state(core.SESS_DIR / f"doubao_{self.sid}.json")
        self.assertTrue(st["pending"])

    def test_explicit_nonfinal_channel_is_excluded(self):
        self.write([{"role": "user", "content": "first"}, {"role": "assistant", "channel": "analysis", "content": "hidden"}, {"role": "assistant", "channel": "commentary", "content": "progress"}, {"role": "assistant", "channel": "final", "content": "final"}])
        result = self.run_capture(dry_run=True)
        self.assertEqual([p["head"] for p in result["preview"]], ["first", "final"])

    def test_system_only_user_does_not_advance_assignment(self):
        self.write([{"role": "user", "content": "first"}, {"role": "user", "content": "<system-reminder>internal</system-reminder>"}, {"role": "assistant", "content": "answer"}, {"role": "user", "content": "second"}])
        out = self.run_capture(dry_run=True)
        self.assertEqual(out["preview"][-1]["timestamp"], doubao.parse_ts("2026-11-01T10:00:00Z"))

    def test_duplicate_user_across_offset_boundary_is_not_replayed(self):
        self.write([{"role": "user", "content": "first"}])
        self.run_capture()
        self.write([{"role": "user", "content": "first"}, {"role": "user", "content": "first"}, {"role": "assistant", "content": "answer"}])
        self.run_capture()
        self.assertEqual([m["role"] for m in self.add.call_args.args[1]], ["assistant"])

    def test_bad_json_is_rejected_not_skipped(self):
        with (self.system / "trajectory.jsonl").open("a") as out:
            out.write('{bad json}\n')
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            self.run_capture(dry_run=True)

    def test_unknown_offset_unit_is_rejected(self):
        core.SESS_DIR.mkdir(parents=True)
        (core.SESS_DIR / f"doubao_{self.sid}.json").write_text(json.dumps({"offset": 2, "offset_unit": "unknown"}))
        with self.assertRaises(ValueError):
            self.run_capture(dry_run=True)


if __name__ == "__main__":
    unittest.main()
