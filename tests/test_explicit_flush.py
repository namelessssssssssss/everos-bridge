"""Extraction policy: acknowledged writes stay buffered until an explicit decision."""
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

from test_core_regressions import IsolatedCore, codex_message, response
import core
import quota

REAL_SWEEP = core.sweep
STEPS = [{"action": str(i), "result": "observed"} for i in range(3)]


class ExplicitFlushTests(IsolatedCore):
    def test_v2_remember_receipt_is_usable_without_implicit_flush(self):
        self.api.return_value = response("accumulated")
        text = core.remember("codex", "decision", "summary")
        target = self.api.call_args.args[1]["session_id"]
        self.assertIn("session_id=" + target, text)
        self.assertEqual(self.api.call_count, 1)
        self.assertIn("deferred", text)
        self.api.return_value = response("extracted")
        out = core.flush_memory("handoff", session_id=target)
        self.assertEqual(self.api.call_args.args[:2], ("/api/v2/memory/flush", {"session_id": target}))
        self.assertIn("flush=extracted", out)
        self.assertIn("搜索可见性须另行核对", out)

    def test_accumulated_practice_does_not_poll_or_flush_but_v1_compat_stays(self):
        self.api.return_value = response("accumulated")
        with patch.object(core, "practice_case_note") as readback:
            out = core.record_practice("codex", "topic", "task", STEPS, "done")
            readback.assert_not_called()
        self.assertEqual([c.args[0] for c in self.api.call_args_list], ["/api/v2/memory/add"])
        self.assertIn("case 未回读", out)
        self.api.reset_mock()
        self.api.side_effect = [response("accumulated"), response("accumulated"), response("extracted")]
        with patch.dict(os.environ, {"EVEROS_READ_V1": "1"}), patch.object(core.time, "sleep"):
            core.record_practice("codex", "topic", "task", STEPS, "done")
        self.assertEqual([c.args[0] for c in self.api.call_args_list],
                         ["/api/v2/memory/add", "/api/v1/memories/agent", "/api/v1/memories/agent/flush"])

    def test_hook_lifecycle_and_forced_sweep_do_not_flush_v2(self):
        payload = self.transcript()
        self.api.return_value = response("accumulated")
        for event in ("Stop", "PreCompact", "SessionEnd"):
            payload["hook_event_name"] = event
            core.write_turns(payload)
        with patch.object(core, "sync_local_memory"):
            REAL_SWEEP(force=True)
        self.assertEqual([c.args[0] for c in self.api.call_args_list], ["/api/v2/memory/add"])
        self.assertTrue(self.ledger()["pending"])
        self.assertTrue(self.api.call_args.args[1]["async_mode"] is False)

    def test_localmem_upload_deduplicates_accepted_content_without_flush(self):
        note = self.local_note()
        self.api.return_value = response("accumulated")
        core.sync_local_memory()
        core.sync_local_memory()
        self.assertEqual(self.api.call_count, 1)
        entry = core.load_state(core.LOCALMEM_LEDGER)[str(note)]
        self.assertEqual(entry["flush"], "deferred")
        self.assertEqual(entry["mtime"], note.stat().st_mtime)

    def test_explicit_conversation_flush_uses_backend_id_and_failure_preserves_state(self):
        core.write_turns(self.transcript())
        before = self.ledger()
        for failure in (response("unknown"), quota.QuotaError("mcu", 0), RuntimeError("offline")):
            self.api.side_effect = failure if isinstance(failure, Exception) else None
            self.api.return_value = failure
            with self.assertRaises(Exception):
                core.flush_memory("handoff", conversation_id="test_session")
            self.assertEqual(self.ledger(), before)
        self.api.side_effect = None
        self.api.return_value = response("no_extraction")
        core.flush_memory("handoff", conversation_id="test_session")
        self.assertEqual(self.api.call_args.args[1]["session_id"], before["everos_session_id"])
        self.assertFalse(self.ledger()["pending"])
        self.assertEqual(self.ledger()["offset"], before["offset"])
        self.api.reset_mock()
        self.assertIn("未发送 flush", core.flush_memory("handoff", conversation_id="test_session"))
        self.api.assert_not_called()

    def test_target_validation_and_first_turn_are_network_free(self):
        invalid = [dict(reason="", session_id="x"), dict(reason="x"),
                   dict(reason="x", session_id="x", conversation_id="y"),
                   dict(reason="x", session_id="x" * 129), dict(reason="x", session_id=" x"),
                   dict(reason="x", conversation_id="../other"), dict(reason="x", conversation_id=".."),
                   dict(reason="x", conversation_id="new-session"),
                   dict(reason="x", session_id="x", api_version="v3")]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                core.flush_memory(**args)
        with patch.dict(os.environ, {"EVEROS_WRITE": "0"}), self.assertRaises(RuntimeError):
            core.flush_memory("complete", session_id="x")
        self.api.assert_not_called()

    def test_backend_and_identity_switch_cannot_flush_old_conversation(self):
        core.write_turns(self.transcript())
        self.api.reset_mock()
        with patch.dict(os.environ, {"EVEROS_USER_ID": "different"}), self.assertRaises(ValueError):
            core.flush_memory("handoff", conversation_id="test_session")
        self.api.assert_not_called()

    def test_equivalent_api_bases_keep_capture_and_conversation_flush(self):
        base = "https://synthetic.example/api"
        for index, (initial, changed) in enumerate(((base, base + "/"), (base + "///", base))):
            with self.subTest(initial=initial, changed=changed):
                payload = self.transcript()
                payload["session_id"] = f"equivalent_{index}"
                state_path = core.SESS_DIR / (payload["session_id"] + ".json")
                with patch.dict(os.environ, {"EVEROS_API_BASE": initial}):
                    core.write_turns(payload)
                    identity = core.load_state(state_path)["identity"]
                with Path(payload["transcript_path"]).open("a") as out:
                    out.write(json.dumps(codex_message("user", "continue")) + "\n")
                with patch.dict(os.environ, {"EVEROS_API_BASE": changed}):
                    core.write_turns(payload)
                    core.flush_memory("handoff", conversation_id=payload["session_id"])
                state = core.load_state(state_path)
                self.assertEqual(state["identity"], identity)
                self.assertEqual(state["messages"], 2)
                self.assertFalse(state["pending"])

    def test_sweep_preserves_identity_mismatched_v1_ledgers_without_network(self):
        for key, value in (("EVEROS_API_BASE", "https://other.example"),
                           ("EVEROS_USER_ID", "other-owner"),
                           ("EVEROS_AGENT_ID", "other-agent")):
            with self.subTest(changed=key):
                state_path = core.SESS_DIR / "test_session.json"
                state_path.unlink(missing_ok=True)
                payload = self.transcript()
                with patch.dict(os.environ, {"EVEROS_API_VERSION": "v1"}):
                    core.write_turns(payload)
                before = state_path.read_bytes()
                self.api.reset_mock()
                self.api.return_value = response("no_extraction")
                with patch.dict(os.environ, {key: value}), patch.object(core, "sync_local_memory"):
                    REAL_SWEEP(force=True)
                self.api.assert_not_called()
                self.assertEqual(state_path.read_bytes(), before)
                self.assertTrue(self.ledger()["pending"])
                state_path.unlink()

    def test_sweep_accepts_matching_and_legacy_unbound_v1_ledgers(self):
        for bound in (False, True):
            state = {"everos_session_id": "legacy", "api": "v1", "pending": True}
            if bound:
                state["identity"] = core._write_identity()
            core.save_state(core.SESS_DIR / "test_session.json", state)
            self.api.reset_mock()
            self.api.return_value = response("no_extraction")
            with patch.object(core, "sync_local_memory"):
                REAL_SWEEP(force=True)
            self.api.assert_called_once()
            self.assertEqual(self.api.call_args.args[0], "/api/v1/memories/flush")
            self.assertFalse(self.ledger()["pending"])

    def test_real_backend_path_change_still_rejects_capture_and_flush(self):
        payload = self.transcript()
        with patch.dict(os.environ, {"EVEROS_API_BASE": "https://synthetic.example/api"}):
            core.write_turns(payload)
        before = self.ledger()
        self.api.reset_mock()
        with patch.dict(os.environ, {"EVEROS_API_BASE": "https://synthetic.example/other"}):
            with self.assertRaises(ValueError):
                core.write_turns(payload)
            with self.assertRaises(ValueError):
                core.flush_memory("handoff", conversation_id="test_session")
        self.api.assert_not_called()
        self.assertEqual(self.ledger(), before)

    def test_v2_to_v1_preserves_old_target_without_forcing_v2_extraction(self):
        payload = self.transcript()
        core.write_turns(payload)
        self.api.reset_mock()
        with Path(payload["transcript_path"]).open("a") as out:
            out.write(json.dumps(codex_message("user", "next turn")) + "\n")
        with patch.dict(os.environ, {"EVEROS_API_VERSION": "v1"}):
            core.write_turns(payload)
        self.assertEqual([c.args[0] for c in self.api.call_args_list], ["/api/v1/memories"])
        self.assertIn("v2", self.ledger()["deferred_flush"])
        self.assertTrue(self.ledger()["pending"])
        core.flush_memory("old handoff", conversation_id="test_session", api_version="v2")
        self.assertNotIn("v2", self.ledger()["deferred_flush"])
        self.assertTrue(self.ledger()["pending"])
        self.assertEqual(self.ledger()["api"], "v1")
        core.flush_memory("finish", conversation_id="test_session")
        self.assertFalse(self.ledger()["pending"])
        self.assertEqual(self.api.call_args.args[0], "/api/v1/memories/flush")

    def test_legacy_lifecycle_and_practice_explicit_route_remain_available(self):
        payload = self.transcript()
        payload["hook_event_name"] = "SessionEnd"
        with patch.dict(os.environ, {"EVEROS_API_VERSION": "v1"}):
            core.write_turns(payload)
        self.assertEqual([c.args[0] for c in self.api.call_args_list],
                         ["/api/v1/memories", "/api/v1/memories/flush"])
        core.flush_memory("complete", session_id="practice_session", api_version="v1", kind="practices")
        self.assertEqual(self.api.call_args.args[0], "/api/v1/memories/agent/flush")

    def test_new_capture_cannot_be_cleared_by_a_concurrent_flush(self):
        payload = self.transcript()
        core.write_turns(payload)
        entered, release, writer_started, writer_done = (threading.Event() for _ in range(4))
        errors = []
        def api(path, body, **kwargs):
            if path.endswith('/flush'):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test release timed out')
            return response('extracted')
        self.api.side_effect = api
        def flush():
            try: core.flush_memory('handoff', conversation_id='test_session')
            except Exception as exc: errors.append(exc)
        def write():
            writer_started.set()
            try: core.write_turns(payload)
            except Exception as exc: errors.append(exc)
            finally: writer_done.set()
        f = threading.Thread(target=flush)
        f.start()
        self.assertTrue(entered.wait(2))
        with Path(payload['transcript_path']).open('a') as out:
            out.write(json.dumps(codex_message('user', 'after flush began')) + '\n')
        w = threading.Thread(target=write)
        w.start()
        try:
            self.assertTrue(writer_started.wait(2))
            self.assertFalse(writer_done.wait(.05))
        finally:
            release.set()
            f.join(3); w.join(3)
        self.assertFalse(errors)
        self.assertFalse(f.is_alive() or w.is_alive())
        self.assertTrue(self.ledger()['pending'])
        self.assertEqual(self.ledger()['messages'], 2)
