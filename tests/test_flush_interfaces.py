"""Offline acceptance for explicit flush tools and CLI; no real configuration or API."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin/everos"))
import cli
import core
import hook
import mcp_server


class FlushInterfaces(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.ready = self.stack.enter_context(mock.patch.object(core, "ready", return_value=True))
        self.config = self.stack.enter_context(mock.patch.object(core, "cfg", return_value={
            "EVEROS_USER_ID": "synthetic-owner", "EVEROS_API_VERSION": "v2"}))
        self.api = self.stack.enter_context(mock.patch.object(core, "api", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(mock.patch.object(core, "log"))
        self.stack.enter_context(mock.patch.object(core, "log_error"))
        self.flush = self.stack.enter_context(mock.patch.object(core, "flush_memory", return_value="confirmed target", create=True))
        self.server = mcp_server.Server()

    def mcp(self, args):
        return self.server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "memory_flush", "arguments": args}})

    def run_cli(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = cli.main(args)
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), errors.getvalue()

    def test_mcp_tool_is_mutating_and_dispatches_exact_target(self):
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "memory_flush")
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        result = self.mcp({"session_id": "receipt-id", "reason": "handoff",
                           "api_version": "v1", "kind": "practices"})
        self.assertFalse(result["result"]["isError"])
        self.flush.assert_called_once_with("handoff", session_id="receipt-id", conversation_id="",
                                          api_version="v1", kind="practices")
        self.api.assert_not_called()

    def test_mcp_native_conversation_id_is_not_passed_as_backend_id(self):
        result = self.mcp({"conversation_id": "native-uuid", "reason": "completed workflow"})
        self.assertFalse(result["result"]["isError"])
        self.flush.assert_called_once_with("completed workflow", session_id="", conversation_id="native-uuid",
                                          api_version=None, kind="episodes")

    def test_mcp_rejects_missing_ambiguous_or_invalid_arguments_before_dispatch(self):
        invalid = [
            {}, {"reason": "done"}, {"session_id": "a"},
            {"session_id": "a", "conversation_id": "b", "reason": "done"},
            {"session_id": "a", "reason": " "},
            {"session_id": "a", "reason": "x" * 501},
            {"session_id": "a" * 129, "reason": "done"},
            {"conversation_id": "a" * 161, "reason": "done"},
            {"session_id": "a", "reason": "done", "api_version": "v3"},
            {"conversation_id": "a", "reason": "done", "kind": "practices"},
        ]
        for args in invalid:
            with self.subTest(args=args):
                result = self.mcp(args)
                self.assertEqual(result["error"]["code"], -32602)
        self.flush.assert_not_called()
        self.api.assert_not_called()

    def test_mcp_write_disable_blocks_flush(self):
        self.config.return_value["EVEROS_WRITE"] = "0"
        result = self.mcp({"session_id": "receipt-id", "reason": "handoff"})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("EVEROS_WRITE=0", result["result"]["content"][0]["text"])
        self.flush.assert_not_called()
        self.api.assert_not_called()

    def test_cli_help_needs_no_configuration_and_lists_both_target_forms(self):
        code, output, errors = self.run_cli(["flush", "--help"])
        self.assertEqual(code, 0)
        for flag in ("--session-id", "--conversation-id", "--reason", "--api-version", "--kind"):
            self.assertIn(flag, output)
        self.ready.assert_not_called()
        self.flush.assert_not_called()
        self.api.assert_not_called()

    def test_cli_parser_rejects_ambiguous_or_missing_targets_and_bad_reason(self):
        invalid = [
            ["flush", "--reason", "done"],
            ["flush", "--session-id", "a"],
            ["flush", "--session-id", "a", "--conversation-id", "b", "--reason", "done"],
            ["flush", "--session-id", "a", "--reason", " "],
            ["flush", "--session-id", "a", "--reason", "x" * 501],
            ["flush", "--conversation-id", "a", "--reason", "done", "--kind", "practices"],
        ]
        for args in invalid:
            with self.subTest(args=args):
                code, _, _ = self.run_cli(args)
                self.assertEqual(code, 2)
        self.ready.assert_not_called()
        self.flush.assert_not_called()
        self.api.assert_not_called()

    def test_cli_dispatches_backend_and_native_targets_without_guessing(self):
        for arguments, expected in (
            (["--session-id", "receipt-id", "--api-version", "v1", "--kind", "practices"],
             dict(session_id="receipt-id", conversation_id="", api_version="v1", kind="practices")),
            (["--conversation-id", "native-uuid"],
             dict(session_id="", conversation_id="native-uuid", api_version=None, kind="episodes")),
        ):
            with self.subTest(arguments=arguments):
                self.flush.reset_mock()
                code, output, errors = self.run_cli(["flush", *arguments, "--reason", " handoff "])
                self.assertEqual((code, errors), (0, ""))
                self.assertIn("confirmed target", output)
                self.flush.assert_called_once_with("handoff", **expected)
        self.api.assert_not_called()

    def test_cli_write_disable_blocks_flush(self):
        self.config.return_value["EVEROS_WRITE"] = "0"
        code, output, errors = self.run_cli(["flush", "--session-id", "receipt-id", "--reason", "done"])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("EVEROS_WRITE=0", errors)
        self.flush.assert_not_called()
        self.api.assert_not_called()

    def test_cli_and_mcp_surface_flush_failure(self):
        self.flush.side_effect = RuntimeError("synthetic refusal")
        code, output, errors = self.run_cli(["flush", "--session-id", "receipt-id", "--reason", "done"])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("synthetic refusal", errors)
        result = self.mcp({"session_id": "receipt-id", "reason": "done"})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("synthetic refusal", result["result"]["content"][0]["text"])

    def test_prompt_hint_uses_native_id_as_conversation_id(self):
        with mock.patch.object(core, "load_state", return_value={}), \
                mock.patch.object(core, "agentic_timeout", return_value=1), \
                mock.patch.object(core, "_num", return_value=2000), \
                mock.patch.object(core, "search_items", return_value=[]), \
                mock.patch.object(core, "format_items_with_ids", return_value=("", [])), \
                mock.patch.object(hook.recall, "save_state"), \
                mock.patch.object(hook.recall, "identity", return_value="synthetic-identity"), \
                mock.patch.object(hook.recall, "record_coverage"):
            blocks = hook.build_prompt_blocks("native-uuid", "current task")
        text = "\n".join(blocks)
        self.assertIn("conversation_id=native-uuid", text)
        self.assertIn("尚未采集", text)
        self.assertIn("reason", text)
        self.api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
