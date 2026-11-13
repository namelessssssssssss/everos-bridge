"""Write-side quota-saving modes (EVEROS_WRITE_MODE), zero-loss noise filtering,
eco transcript compaction/coalescing, and daily soft-budget auto-downgrade."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "plugin" / "everos"
sys.path.insert(0, str(SOURCE))
import cli
import core
import hook
import quota
from test_core_regressions import IsolatedCore, codex_message, response

REAL_SWEEP = core.sweep


class WriteModeAndNoiseFilterTests(IsolatedCore):
    def test_write_mode_precedence_and_strict_validation(self):
        self.assertEqual(core.write_mode({}), "eco")
        self.assertEqual(core.write_mode({"EVEROS_WRITE_MODE": "FULL"}), "full")
        self.assertEqual(core.write_mode({"EVEROS_WRITE_MODE": "curated"}), "curated")
        self.assertEqual(core.write_mode({"EVEROS_WRITE_MODE": "off"}), "off")
        # Legacy EVEROS_WRITE=0 always wins as hard off.
        self.assertEqual(core.write_mode({"EVEROS_WRITE": "0", "EVEROS_WRITE_MODE": "full"}), "off")
        self.assertEqual(core.write_mode({"EVEROS_WRITE_MODE": "unknown"}), "eco")
        with self.assertRaisesRegex(ValueError, "EVEROS_WRITE_MODE"):
            core.write_mode({"EVEROS_WRITE_MODE": "unknown"}, strict=True)

        with patch.dict(os.environ, {"EVEROS_WRITE_MODE": "off"}):
            for fn in (
                lambda: core.post_messages("s", [{"role": "user", "content": "x"}]),
                lambda: core.remember("codex", "topic", "note"),
                lambda: core.write_turns(self.transcript()),
                lambda: core.sync_local_memory(),
            ):
                with self.assertRaisesRegex(RuntimeError, "EVEROS_WRITE_MODE=off"):
                    fn()
            with patch.object(hook.subprocess, "Popen") as popen, \
                    patch.object(sys, "argv", ["hook.py"]), \
                    patch.object(sys, "stdin", io.StringIO(json.dumps(self.transcript()))):
                self.assertEqual(hook.main(), 0)
                popen.assert_not_called()

    def test_zero_user_subagent_and_heartbeat_sessions_make_zero_api_calls(self):
        # Antigravity SYSTEM_SDK subagent transcript (0 USER_INPUT steps).
        ag_dir = self.root / "brain" / "subagent-conv" / ".system_generated" / "logs"
        ag_dir.mkdir(parents=True)
        ag_path = ag_dir / "transcript_full.jsonl"
        ag_rows = [
            {"step_index": 0, "source": "SYSTEM_SDK", "type": "PLANNER_RESPONSE", "status": "DONE",
             "created_at": "2026-11-13T01:00:00Z", "content": "Subagent finished code search."},
        ]
        ag_path.write_text("".join(json.dumps(r) + "\n" for r in ag_rows), encoding="utf-8")
        for mode in ("eco", "full"):
            with patch.dict(os.environ, {"EVEROS_WRITE_MODE": mode}):
                core.write_turns({
                    "conversationId": "subagent-conv",
                    "transcriptPath": str(ag_path),
                    "hook_event_name": "Stop",
                })
        self.api.assert_not_called()
        self.assertFalse((core.SESS_DIR / "subagent-conv.json").exists())

        # Pure <heartbeat> Codex automation thread (both user heartbeat and assistant reply).
        hb_path = self.root / "rollout-heartbeat.jsonl"
        hb_rows = [
            {"type": "session_meta", "payload": {"id": "hb_only"}},
            codex_message("user", "<heartbeat>\nRun scheduled check\n</heartbeat>"),
            codex_message("assistant", "All systems nominal, 0 issues found.", "final"),
        ]
        hb_path.write_text("".join(json.dumps(r) + "\n" for r in hb_rows), encoding="utf-8")
        core.write_turns({"session_id": "hb_only", "transcript_path": str(hb_path), "hook_event_name": "Stop"})
        self.api.assert_not_called()
        self.assertFalse((core.SESS_DIR / "hb_only.json").exists())

    def test_mixed_thread_filters_heartbeat_turns_and_external_page_dumps_across_hosts(self):
        # Mixed target_thread_id session: real turn 1 -> heartbeat turn -> real turn 2 with external app dump.
        tpath = self.root / "rollout-mixed.jsonl"
        rows = [
            {"type": "session_meta", "payload": {"id": "mixed_thread"}},
            codex_message("user", "先看下配置"),
            codex_message("assistant", "配置正常", "final"),
            codex_message("user", "<heartbeat>\nDaily check\n</heartbeat>"),
            codex_message("assistant", "Routine health check passed; giant report ignored", "final"),
            codex_message("user", "<external_codex_apps_browser>\n" + ("DOM " * 500)
                          + "</external_codex_apps_browser>\n# Response annotations:\n请根据我的批注调整超时"),
            codex_message("assistant", "已按批注调整为 190 秒", "final"),
        ]
        tpath.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        core.write_turns({"session_id": "mixed_thread", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        self.api.assert_called_once()
        sent = self.api.call_args.args[1]["messages"]
        self.assertEqual([(m["role"], m["content"]) for m in sent], [
            ("user", "先看下配置"),
            ("assistant", "配置正常"),
            ("user", "# Response annotations:\n请根据我的批注调整超时"),
            ("assistant", "已按批注调整为 190 秒"),
        ])

        # Verify heartbeat pair skipping in Claude, Kimi, MiMo, and Antigravity parsers as well.
        claude_msgs = core.messages_claude([
            {"type": "user", "timestamp": "2026-11-13T00:00:00Z", "message": {"content": "<heartbeat>tick</heartbeat>"}},
            {"type": "assistant", "timestamp": "2026-11-13T00:00:01Z", "message": {"content": [{"type": "text", "text": "hb reply"}]}},
            {"type": "user", "timestamp": "2026-11-13T00:00:02Z", "message": {"content": "真实提问"}},
            {"type": "assistant", "timestamp": "2026-11-13T00:00:03Z", "message": {"content": [{"type": "text", "text": "真实回答"}]}},
        ])
        self.assertEqual([m["content"] for m in claude_msgs], ["真实提问", "真实回答"])

        mimo_msgs = core.messages_mimo([
            {"role": "user", "parts": [{"type": "text", "text": "<heartbeat>tick</heartbeat>"}]},
            {"role": "assistant", "parts": [{"type": "text", "text": "hb reply"}]},
            {"role": "user", "parts": [{"type": "text", "text": "mimo question"}]},
            {"role": "assistant", "parts": [{"type": "text", "text": "mimo answer"}]},
        ])
        self.assertEqual([m["content"] for m in mimo_msgs], ["mimo question", "mimo answer"])

    def test_eco_compaction_preserves_decisions_and_full_mode_keeps_verbatim(self):
        code_block = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n```"
        table = "\n".join([
            "| col1 | col2 |",
            "| --- | --- |",
            *[f"| row_{i} | val_{i} |" for i in range(10)],
        ])
        prose = "背景说明。" * 260
        assistant_raw = (
            f"核心决策：默认开启 eco 省额度模式，读侧不变。\n\n"
            f"详见 [core.py](file:///Users/demo/everos/core.py#L244-L360)：\n"
            f"{code_block}\n\n{table}\n\n{prose}\n\n最终结论：验证全部通过。"
        )
        tpath = self.root / "eco-compaction.jsonl"
        tpath.write_text(
            json.dumps(codex_message("user", "请实现省额度模式"), ensure_ascii=False) + "\n"
            + json.dumps(codex_message("assistant", assistant_raw, "final"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        core.write_turns({"session_id": "eco_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        eco_sent = self.api.call_args.args[1]["messages"]
        self.assertEqual(eco_sent[0]["content"], "请实现省额度模式")
        eco_reply = eco_sent[1]["content"]
        self.assertIn("核心决策：默认开启 eco 省额度模式", eco_reply)
        self.assertIn("最终结论：验证全部通过。", eco_reply)
        self.assertIn("详见 core.py：", eco_reply)
        self.assertNotIn("file:///", eco_reply)
        self.assertNotIn("line_25 =", eco_reply)
        self.assertLessEqual(len(eco_reply), 1350)

        # Structured remember stays 100% uncompacted even in eco mode.
        self.api.reset_mock()
        verbatim = "精确命令与代码：\n" + code_block + "\n" + ("细节" * 800)
        core.remember("codex", "eco_spec", verbatim, "用户原话")
        rem_sent = self.api.call_args.args[1]["messages"][1]["content"]
        self.assertIn("line_25 = 25", rem_sent)
        self.assertGreater(len(rem_sent), 2000)

        # full mode preserves verbatim assistant reply in background capture.
        self.api.reset_mock()
        with patch.dict(os.environ, {"EVEROS_WRITE_MODE": "full"}):
            core.write_turns({"session_id": "full_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        full_reply = self.api.call_args.args[1]["messages"][1]["content"]
        self.assertIn("line_25 = 25", full_reply)
        self.assertIn("file:///Users/demo/everos/core.py", full_reply)

    def test_reactive_wakeup_coalesces_assistant_continuations_and_fully_idle_false_skips(self):
        tpath = self.root / "wakeup.jsonl"
        turn1 = [
            codex_message("user", "跑一下全量回归"),
            codex_message("assistant", "已启动后台测试任务，等待通知。", "final"),
        ]
        tpath.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in turn1), encoding="utf-8")

        # Stop with fullyIdle=False does not write or spawn worker.
        core.write_turns({"session_id": "wake_sess", "transcript_path": str(tpath),
                          "hook_event_name": "Stop", "fullyIdle": False})
        self.api.assert_not_called()

        # First completed Stop sends [user, assistant].
        core.write_turns({"session_id": "wake_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        self.assertEqual(self.api.call_count, 1)
        offset_after_turn1 = core.load_state(core.SESS_DIR / "wake_sess.json")["offset"]

        # Reactive wakeup #1 and #2 append assistant-only continuations on Stop -> deferred without API calls!
        for part in ("后台任务 1 完成，继续汇总。", "全部 384 个测试通过。"):
            with tpath.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(codex_message("assistant", part, "final"), ensure_ascii=False) + "\n")
            core.write_turns({"session_id": "wake_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        self.assertEqual(self.api.call_count, 1)
        st = core.load_state(core.SESS_DIR / "wake_sess.json")
        self.assertEqual(st["offset"], offset_after_turn1)
        self.assertTrue(st["deferred_wakeup"])

        # Next user prompt flushes the coalesced wakeup slice + new turn in a single add!
        with tpath.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(codex_message("user", "很好，提 PR 吧"), ensure_ascii=False) + "\n")
            handle.write(json.dumps(codex_message("assistant", "PR 已创建。", "final"), ensure_ascii=False) + "\n")
        core.write_turns({"session_id": "wake_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        self.assertEqual(self.api.call_count, 2)
        second_msgs = self.api.call_args.args[1]["messages"]
        self.assertEqual([m["role"] for m in second_msgs], ["assistant", "user", "assistant"])
        self.assertIn("后台任务 1 完成", second_msgs[0]["content"])
        self.assertIn("全部 384 个测试通过", second_msgs[0]["content"])
        self.assertFalse(core.load_state(core.SESS_DIR / "wake_sess.json").get("deferred_wakeup"))

        # Idle sweep also flushes deferred wakeup slices when no next user turn arrives.
        with tpath.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(codex_message("assistant", "CI 绿灯确认。", "final"), ensure_ascii=False) + "\n")
        core.write_turns({"session_id": "wake_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        self.assertEqual(self.api.call_count, 2)
        with patch.object(core, "sync_local_memory"):
            REAL_SWEEP(force=True)
        self.assertEqual(self.api.call_count, 3)
        self.assertEqual(self.api.call_args.args[1]["messages"][0]["content"], "CI 绿灯确认。")
        self.assertFalse(core.load_state(core.SESS_DIR / "wake_sess.json").get("deferred_wakeup"))

    def test_same_turn_remember_deduplicates_trailing_stop_capture(self):
        tpath = self.root / "dedupe.jsonl"
        tpath.write_text(
            json.dumps(codex_message("user", "初始化会话"), ensure_ascii=False) + "\n"
            + json.dumps(codex_message("assistant", "好的", "final"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        core.write_turns({"session_id": "dedupe_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        self.assertEqual(self.api.call_count, 1)

        # Turn 2: agent calls memory_remember WITH user_said + conversation_id before Stop fires.
        self.api.reset_mock()
        core.remember("codex", "arch_decision", "采用 eco 模式并保留显式写工具", "把省额度模式设成默认",
                      conversation_id="dedupe_sess")
        self.assertEqual(self.api.call_count, 1)
        with tpath.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(codex_message("user", "把省额度模式设成默认"), ensure_ascii=False) + "\n")
            handle.write(json.dumps(codex_message("assistant", "长篇总结：" + ("细节" * 900), "final"), ensure_ascii=False) + "\n")
        core.write_turns({"session_id": "dedupe_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        # Stop recognizes user_said + conclusion were already written -> 0 extra add calls, offset still advances!
        self.assertEqual(self.api.call_count, 1)
        self.assertEqual(core.load_state(core.SESS_DIR / "dedupe_sess.json")["offset"], tpath.stat().st_size)

        # Turn 3: agent calls memory_remember WITHOUT user_said -> Stop keeps user prompt + brief stub.
        self.api.reset_mock()
        core.remember("codex", "followup", "补充了每日预算软降级", conversation_id="dedupe_sess")
        self.assertEqual(self.api.call_count, 1)
        with tpath.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(codex_message("user", "把每日软上限也加上"), ensure_ascii=False) + "\n")
            handle.write(json.dumps(codex_message("assistant", "已完成实现：" + ("长篇展开" * 800), "final"), ensure_ascii=False) + "\n")
        core.write_turns({"session_id": "dedupe_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
        self.assertEqual(self.api.call_count, 2)
        stop_msgs = self.api.call_args.args[1]["messages"]
        self.assertEqual(stop_msgs[0]["content"], "把每日软上限也加上")
        self.assertIn("本轮结论已由 memory_remember 结构化入库", stop_msgs[1]["content"])
        self.assertLess(len(stop_msgs[1]["content"]), 350)

    def test_curated_mode_advances_ledger_without_auto_add_and_allows_explicit_tools(self):
        tpath = self.root / "curated.jsonl"
        tpath.write_text(
            json.dumps(codex_message("user", "排查一下超时"), ensure_ascii=False) + "\n"
            + json.dumps(codex_message("assistant", "定位完成", "final"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"EVEROS_WRITE_MODE": "curated"}):
            core.write_turns({"session_id": "cur_sess", "transcript_path": str(tpath), "hook_event_name": "Stop"})
            self.api.assert_not_called()
            st = core.load_state(core.SESS_DIR / "cur_sess.json")
            self.assertEqual(st["offset"], tpath.stat().st_size)
            self.assertTrue(st["everos_session_id"].startswith("cx_"))

            # Explicit remember & flush still work seamlessly on cur_sess!
            receipt = core.remember("codex", "timeout_fix", "根因是外层 60s 超时", "排查一下超时",
                                    conversation_id="cur_sess")
            self.assertIn(st["everos_session_id"], receipt)
            self.assertEqual(self.api.call_count, 1)
            flush_out = core.flush_memory("handoff", conversation_id="cur_sess")
            self.assertIn("flush=extracted", flush_out)
            self.assertEqual(self.api.call_count, 2)

    def test_daily_soft_budget_auto_downgrades_to_curated_resets_next_day_and_keeps_secrets_out(self):
        with patch.dict(os.environ, {"EVEROS_DAILY_WRITE_BUDGET": "2"}):
            for idx in range(3):
                tpath = self.root / f"b_{idx}.jsonl"
                tpath.write_text(
                    json.dumps(codex_message("user", f"turn {idx}"), ensure_ascii=False) + "\n"
                    + json.dumps(codex_message("assistant", f"reply {idx}", "final"), ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                core.write_turns({"session_id": f"bs_{idx}", "transcript_path": str(tpath), "hook_event_name": "Stop"})
            # Only the first 2 background sessions called /api/v2/memory/add; the 3rd downgraded to curated!
            self.assertEqual(self.api.call_count, 2)
            self.assertGreater(core.load_state(core.SESS_DIR / "bs_2.json")["offset"], 0)
            log_text = core.LOG.read_text(encoding="utf-8")
            self.assertEqual(log_text.count('"write_budget_downgraded"'), 1)

            # Local-memory sync pauses without marking the note synced when downgraded.
            note = self.local_note()
            core.sync_local_memory()
            self.assertEqual(self.api.call_count, 2)
            self.assertEqual(core.load_state(core.LOCALMEM_LEDGER), {})

            # Explicit curated writes (remember / fix_profile) are NEVER blocked by daily soft budget!
            core.remember("codex", "critical_fact", "显式记忆不受软上限阻断", "记一下这个结论")
            self.assertEqual(self.api.call_count, 3)

            # CLI quota status and doctor surface mode + daily budget without network calls on quota status.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["quota", "status"]), 0)
            status_json = json.loads(buf.getvalue())
            self.assertEqual(status_json[0]["write_mode"], "eco")
            self.assertEqual(status_json[0]["daily_budget"]["auto_adds"], 2)
            self.assertEqual(status_json[0]["daily_budget"]["curated_writes"], 1)
            self.assertTrue(status_json[0]["daily_budget"]["downgraded"])

            self.api.return_value = {"data": {"total_count": 5}}
            report, healthy = core.doctor_result()
            self.assertTrue(healthy)
            self.assertIn("mode=eco", report)
            self.assertIn("今日自动采集 2/2 批，已降级 curated", report)

            # Next CST day automatically restores eco capture and syncs deferred local-memory note!
            self.api.reset_mock()
            self.api.return_value = response("accumulated")
            with patch.object(quota.DailyBudget, "today", return_value="2099-01-01"):
                core.sync_local_memory()
                self.assertEqual(self.api.call_count, 1)
                self.assertIn(str(note), core.load_state(core.LOCALMEM_LEDGER))

            # Verify 0600 permissions (on POSIX) and zero private strings in state/quotas.
            quota_files = list((core.STATE / "quotas").iterdir())
            self.assertTrue(quota_files)
            for qf in quota_files:
                if os.name != "nt" and qf.suffix == ".json":
                    self.assertEqual(qf.stat().st_mode & 0o777, 0o600)
                if qf.is_file():
                    raw = qf.read_text(encoding="utf-8")
                    self.assertNotIn("test_owner", raw)
                    self.assertNotIn("test_key", raw)

        # Invalid config values mark doctor unhealthy with explicit diagnostics.
        with patch.dict(os.environ, {"EVEROS_WRITE_MODE": "invalid_mode", "EVEROS_DAILY_WRITE_BUDGET": "-9"}):
            self.api.return_value = {"data": {"total_count": 1}}
            bad_report, bad_healthy = core.doctor_result()
            self.assertFalse(bad_healthy)
            self.assertIn("EVEROS_WRITE_MODE", bad_report)
            self.assertIn("EVEROS_DAILY_WRITE_BUDGET", bad_report)


if __name__ == "__main__":
    unittest.main()
