"""Google Antigravity transcript, incremental collection and lifecycle regressions."""
import contextlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_core_regressions import IsolatedCore, response, core, hook
import mcp_server


def user_step(step_index, text, wrapped=True, created_at="2026-11-10T02:00:00Z", status="DONE"):
    content = (
        f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-10\n</ADDITIONAL_METADATA>\n"
        "<USER_SETTINGS_CHANGE>\nThe user has changed the model\n</USER_SETTINGS_CHANGE>"
        if wrapped else text
    )
    return {
        "step_index": step_index,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": status,
        "created_at": created_at,
        "content": content,
    }


def planner_step(step_index, content="", tool_calls=None, created_at="2026-11-10T02:00:05Z", status="DONE"):
    step = {
        "step_index": step_index,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": status,
        "created_at": created_at,
        "thinking": "internal reasoning",
    }
    if content:
        step["content"] = content
    if tool_calls is not None:
        step["tool_calls"] = tool_calls
    return step


class AntigravityTests(IsolatedCore):
    def setUp(self):
        super().setUp()
        self.home = self.root.resolve() / "home"
        self.logs = (
            self.home / ".gemini/antigravity/brain/01145a13-411a-40e1-aa3f-92645591a2a4/.system_generated/logs"
        )
        self.logs.mkdir(parents=True, exist_ok=True)
        self.stack.enter_context(patch.dict(os.environ, {
            "EVEROS_INSTALL_HOME": str(self.home),
            "EVEROS_HOST": "antigravity",
        }))
        self.stack.enter_context(patch.object(hook, "HOST_OVERRIDE", "antigravity"))

    def write_transcripts(self, compact_entries, full_entries=None):
        compact = self.logs / "transcript.jsonl"
        compact.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in compact_entries))
        if full_entries is not None:
            full = self.logs / "transcript_full.jsonl"
            full.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in full_entries))
        return compact

    def lifecycle(self, event, **kwargs):
        payload = {
            "conversationId": "01145a13-411a-40e1-aa3f-92645591a2a4",
            "transcriptPath": str(self.logs / "transcript.jsonl"),
            "workspacePaths": [str(self.root / "novel_ug")],
            **kwargs,
        }
        out = io.StringIO()
        with patch.dict(os.environ, {"EVEROS_HOOK_EVENT": event}), \
                patch("sys.argv", ["hook.py"]), \
                patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out):
            self.assertEqual(hook.main(), 0)
        return json.loads(out.getvalue())

    def test_dialogue_parses_user_request_planner_responses_and_botmux(self):
        rows = [
            user_step(0, "被重试覆盖的提问", created_at="2026-11-10T01:59:00Z"),
            user_step(0, "接入 everos-bridge 并提 PR"),
            planner_step(1, tool_calls=[{"name": "run_command", "args": {"CommandLine": "git status"}}]),
            {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE",
             "created_at": "2026-11-10T02:00:06Z", "content": "On branch main"},
            planner_step(3, content="已完成调查", tool_calls=[
                {"name": "run_command", "args": {"CommandLine": "botmux send --chat oc_1 '进度正常'"}}
            ]),
            planner_step(4, content="错误步骤", status="ERROR"),
            planner_step(5, content="最终结论"),
        ]
        msgs = core.messages_antigravity(rows)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0]["content"], "接入 everos-bridge 并提 PR")
        self.assertEqual(msgs[1]["content"], "已完成调查\n\n（经 botmux 发到飞书）进度正常\n\n最终结论")
        self.assertEqual(msgs[1]["sender_id"], "antigravity")
        self.assertLess(msgs[0]["timestamp"], msgs[1]["timestamp"])

    def test_prefers_untruncated_transcript_full_and_supports_dry_run(self):
        compact = self.write_transcripts(
            [
                user_step(0, "截断问题"),
                {**planner_step(1, content="short...[truncated]"), "truncated_fields": ["content"]},
            ],
            [
                user_step(0, "截断问题"),
                planner_step(1, content="完整未截断的助手长回复内容"),
            ],
        )
        self.assertEqual(core.detect_host([], str(compact)), "antigravity")
        core.write_turns({
            "conversationId": "01145a13-411a-40e1-aa3f-92645591a2a4",
            "transcriptPath": str(compact),
            "workspacePaths": ["/workspace/everos-bridge"],
        })
        ledger = core.load_state(core.SESS_DIR / "01145a13-411a-40e1-aa3f-92645591a2a4.json")
        self.assertEqual(ledger["host"], "antigravity")
        self.assertEqual(ledger["everos_session_id"], "ag_20261110_everos_bridge_01145a13")
        self.assertTrue(ledger["transcript_path"].endswith("transcript_full.jsonl"))
        sent = self.api.call_args.args[1]["messages"]
        self.assertEqual([m["content"] for m in sent], ["截断问题", "完整未截断的助手长回复内容"])

        out = io.StringIO()
        with patch("sys.argv", ["hook.py", "--dry-run", str(compact)]), contextlib.redirect_stdout(out):
            self.assertEqual(hook.main(), 0)
        self.assertIn("ag_20261110_x_01145a13", out.getvalue())
        self.assertIn("完整未截断的助手长回复内容", out.getvalue())

    def test_session_start_pre_invocation_dedup_post_tool_reminder_and_checkpoint_recovery(self):
        self.write_transcripts([user_step(0, "首轮问题")])
        with patch.object(core, "profile_text", return_value="偏好简洁"):
            start = self.lifecycle("SessionStart")
            self.assertIn("偏好简洁", start["injectSteps"][0]["systemMessage"]["systemMessage"])
            duplicate_start = self.lifecycle("SessionStart")
            self.assertEqual(duplicate_start, {})

        items = [{"id": "m1", "ref": "em_" + "a" * 40, "kind": "episode", "text": "历史记忆一",
                  "date": "2026-11-09", "source": "v2/episode", "truncated": False}]
        with patch.object(core, "search_items", return_value=items) as search_mock, \
                patch.object(core, "profile_text", return_value="偏好简洁"):
            first = self.lifecycle(
                "PreInvocation",
                invocationNum=1,
                initialNumSteps=2,
                lastUserInput="<USER_REQUEST>\n首轮问题\n</USER_REQUEST>",
            )
            ephemeral = first["injectSteps"][0]["ephemeralMessage"]
            self.assertIn("历史记忆一", ephemeral)
            self.assertIn("conversation_id=01145a13-411a-40e1-aa3f-92645591a2a4", ephemeral)
            self.assertEqual(search_mock.call_count, 1)

            # Tool-loop continuation in the same turn does not re-run network search.
            cont = self.lifecycle(
                "PreInvocation",
                invocationNum=2,
                initialNumSteps=2,
                lastUserInput="<USER_REQUEST>\n首轮问题\n</USER_REQUEST>",
            )
            self.assertEqual(cont, {})
            self.assertEqual(search_mock.call_count, 1)

            # PostToolUse queues a local error reminder and returns {} without overwriting tool output.
            post_tool = self.lifecycle(
                "PostToolUse",
                toolCall={"name": "run_command", "args": {"CommandLine": "pytest"}},
                error="exit status 1",
                result="Traceback: AssertionError: failed",
            )
            self.assertEqual(post_tool, {})

            after_error = self.lifecycle(
                "PreInvocation",
                invocationNum=3,
                initialNumSteps=2,
                lastUserInput="<USER_REQUEST>\n首轮问题\n</USER_REQUEST>",
            )
            self.assertIn("【EverOS 任务中检索提醒】", after_error["injectSteps"][0]["ephemeralMessage"])
            self.assertEqual(search_mock.call_count, 1)

            # Compaction checkpoint in transcript triggers profile + policy restoration on next turn.
            self.write_transcripts([
                user_step(0, "首轮问题"),
                {"step_index": 10, "source": "SYSTEM", "type": "CHECKPOINT", "status": "DONE",
                 "created_at": "2026-11-10T02:10:00Z", "content": "summary"},
            ])
            recovered = self.lifecycle(
                "PreInvocation",
                invocationNum=1,
                initialNumSteps=12,
                lastUserInput="<USER_REQUEST>\n压缩后继续\n</USER_REQUEST>",
            )
            recovered_msg = recovered["injectSteps"][0]["ephemeralMessage"]
            self.assertIn("【EverOS 用户画像】", recovered_msg)
            self.assertIn("【EverOS 检索用法】", recovered_msg)
            self.assertEqual(search_mock.call_count, 2)

    def test_mcp_host_label_recognizes_antigravity_and_agy(self):
        self.assertEqual(mcp_server.host_label("antigravity-cli"), "antigravity")
        self.assertEqual(mcp_server.host_label("Google Antigravity"), "antigravity")
        self.assertEqual(mcp_server.host_label("agy"), "antigravity")
