"""Offline MCP regressions; no credentials, user configuration, or remote API calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin" / "everos"))
import mcp_server  # noqa: E402


def request(method: str, params=None, mid=1) -> dict:
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class McpTests(unittest.TestCase):
    def setUp(self):
        self.server = mcp_server.Server()
        for patcher in (
            mock.patch.object(mcp_server.core, "ready", return_value=True),
            mock.patch.object(mcp_server.core, "cfg", return_value={
                "EVEROS_USER_ID": "unit-owner", "EVEROS_API_VERSION": "v2"}),
            mock.patch.object(mcp_server.core, "api", side_effect=AssertionError("Unexpected API call")),
            mock.patch.object(mcp_server.core, "log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def call_tool(self, name, args) -> dict:
        return self.server.respond(request("tools/call", {"name": name, "arguments": args}))

    def assert_invalid_params(self, response):
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["id"], 1)
        self.assertNotIn("result", response)

    def test_initialize_and_six_tools_remain_compatible(self):
        result = self.server.respond(request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "Codex", "version": "test"}}))["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(result["serverInfo"]["name"], "everos")
        self.assertIn("每条提问前", result["instructions"])
        self.assertNotIn("时再检索", result["instructions"])
        self.assertEqual(self.server.host, "codex")
        tools = self.server.respond(request("tools/list"))["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {
            "memory_profile", "memory_search", "memory_recent", "memory_remember",
            "memory_record_practice", "memory_fix_profile"})

    def test_invalid_envelopes_return_invalid_request(self):
        invalid = [None, [], 3, "text", {}, {"jsonrpc": "2.0", "id": 1},
                   {"jsonrpc": "1.0", "id": 1, "method": "ping"},
                   {"jsonrpc": "2.0", "id": 1, "method": 4},
                   {"jsonrpc": "2.0", "id": 1, "method": "ping", "result": {}}]
        invalid += [request("ping", mid=mid) for mid in (None, True, 1.5, [], {})]
        for frame in invalid:
            with self.subTest(frame=frame):
                response = self.server.respond(frame)
                self.assertEqual(response["error"]["code"], -32600)
                self.assertIsNone(response["id"])

    def test_notifications_and_valid_peer_responses_do_not_reply(self):
        frames = [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
            {"jsonrpc": "2.0", "method": "extension/notification", "params": {}},
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "Unsupported"}},
        ]
        for frame in frames:
            with self.subTest(frame=frame):
                self.assertIsNone(self.server.respond(frame))
        with mock.patch.object(mcp_server.core, "remember") as remember:
            self.assertIsNone(self.server.respond({"jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": "memory_remember", "arguments": {"topic": "x", "content": "y"}}}))
            remember.assert_not_called()

    def test_string_and_integer_ids_are_preserved(self):
        for mid in (0, "", "request-7", 12):
            with self.subTest(mid=mid):
                self.assertEqual(self.server.respond(request("ping", mid=mid)),
                                 {"jsonrpc": "2.0", "id": mid, "result": {}})

    def test_unknown_method_is_protocol_error(self):
        response = self.server.respond(request("no/such/method"))
        self.assertEqual(response["error"]["code"], -32601)
        self.assertEqual(response["id"], 1)

    def test_nonobject_params_and_invalid_initialize_are_protocol_errors(self):
        for params in (None, [], "text", False):
            with self.subTest(params=params):
                frame = request("ping")
                frame["params"] = params
                self.assert_invalid_params(self.server.respond(frame))
        for params in ({"clientInfo": "Codex"}, {"clientInfo": {"name": 3}},
                       {"protocolVersion": []}, {"capabilities": False}):
            with self.subTest(params=params):
                self.assert_invalid_params(self.server.respond(request("initialize", params)))

    def test_unknown_tools_and_argument_shapes_are_protocol_errors(self):
        frames = [{}, {"name": "unknown", "arguments": {}}, {"name": 5, "arguments": {}},
                  {"name": "memory_search"}, {"name": "memory_profile", "arguments": None},
                  {"name": "memory_profile", "arguments": []}]
        for params in frames:
            with self.subTest(params=params):
                self.assert_invalid_params(self.server.respond(request("tools/call", params)))

    def test_search_and_recent_validate_types_bounds_and_enum(self):
        bad_search = [{}, {"query": 4}, {"query": " "}, {"query": "x", "kind": "profiles"},
                      {"query": "x", "top_k": True}, {"query": "x", "top_k": 0},
                      {"query": "x", "top_k": -2}, {"query": "x", "top_k": 101},
                      {"query": "x", "top_k": 2.5}, {"query": "x", "top_k": "100"},
                      {"query": "x", "method": "vector"}, {"query": "x", "method": "keyword"},
                      {"query": "x", "method": "Agentic"}, {"query": "x", "method": ""},
                      {"query": "x", "method": None}, {"query": "x", "method": False}]
        bad_recent = [{"days": 0}, {"days": 91}, {"days": False}, {"limit": 101}, {"limit": "2"}]
        for name, cases in (("memory_search", bad_search), ("memory_recent", bad_recent)):
            for args in cases:
                with self.subTest(name=name, args=args):
                    self.assert_invalid_params(self.call_tool(name, args))

    def test_search_accepts_official_top_k_range_and_passes_it_unchanged(self):
        for top_k in (-1, *range(1, 101)):
            with self.subTest(top_k=top_k), \
                    mock.patch.object(mcp_server.core, "search_text", return_value="search") as search:
                response = self.call_tool("memory_search", {"query": "task", "top_k": top_k})
                self.assertFalse(response["result"]["isError"])
                search.assert_called_once_with("task", top_k, "all", method="hybrid")

    def test_search_schema_advertises_opt_in_agentic_and_keeps_hybrid_default(self):
        tools = self.server.respond(request("tools/list"))["result"]["tools"]
        search = next(tool for tool in tools if tool["name"] == "memory_search")
        schema = search["inputSchema"]
        self.assertNotIn("method", schema["required"])
        self.assertEqual(schema["properties"]["method"]["type"], "string")
        self.assertEqual(set(schema["properties"]["method"]["enum"]), {"hybrid", "agentic"})
        self.assertEqual(schema["properties"]["method"]["default"], "hybrid")

    def test_search_timeout_schema_is_optional_positive_number_without_a_default(self):
        tools = self.server.respond(request("tools/list"))["result"]["tools"]
        schema = next(tool for tool in tools if tool["name"] == "memory_search")["inputSchema"]
        self.assertNotIn("timeout", schema["required"])
        self.assertEqual(schema["properties"]["timeout"]["type"], "number")
        self.assertEqual(schema["properties"]["timeout"]["exclusiveMinimum"], 0)
        self.assertNotIn("default", schema["properties"]["timeout"])

    def test_search_timeout_passes_positive_integer_and_fractional_seconds_as_budget(self):
        for method in ("hybrid", "agentic"):
            for timeout in (.25, 180, 240.5):
                with self.subTest(method=method, timeout=timeout), \
                        mock.patch.object(mcp_server.core, "search_text", return_value="found") as search:
                    response = self.call_tool("memory_search", {
                        "query": "historical question", "kind": "practices", "top_k": 100,
                        "method": method, "timeout": timeout})
                self.assertFalse(response["result"]["isError"])
                search.assert_called_once_with("historical question", 100, "practices",
                                               method=method, budget=timeout)

    def test_invalid_search_timeout_is_rejected_before_dispatch(self):
        for timeout in (0, -1, 10 ** 400, "180", None, True, False, [], {},
                        float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout), \
                    mock.patch.object(mcp_server.core, "search_text") as search:
                self.assert_invalid_params(self.call_tool("memory_search", {
                    "query": "historical question", "method": "agentic", "timeout": timeout}))
                search.assert_not_called()

    def test_search_methods_preserve_kind_and_server_sized_or_large_top_k(self):
        for method in ("hybrid", "agentic"):
            for kind in ("all", "episodes", "practices"):
                for top_k in (-1, 21, 100):
                    with self.subTest(method=method, kind=kind, top_k=top_k), \
                            mock.patch.object(mcp_server.core, "search_text", return_value="found") as search:
                        response = self.call_tool("memory_search", {
                            "query": "implicit historical question", "top_k": top_k,
                            "kind": kind, "method": method})
                        self.assertFalse(response["result"]["isError"])
                        search.assert_called_once_with("implicit historical question", top_k, kind, method=method)

    def test_invalid_method_is_rejected_before_dispatch(self):
        with mock.patch.object(mcp_server.core, "search_text") as search:
            self.assert_invalid_params(self.call_tool("memory_search", {"query": "task", "method": "unknown"}))
            search.assert_not_called()

    def test_blank_agent_entries_fall_back_and_never_render_as_arrow_or_colon(self):
        def fake_api(path, body, timeout):
            blank_case = {"task_intent": " \n", "key_insight": "\t", "score": .9}
            blank_skill = {"name": " ", "description": "\n", "score": .9}
            cases, skills = [{}, blank_case], [{}, blank_skill]
            if body["method"] == "hybrid":
                cases.append({"id": "case", "task_intent": "real task", "key_insight": "real insight", "score": .9})
                skills.append({"id": "skill", "name": "real skill", "description": "real description", "score": .9})
            return {"data": {"agent_cases": cases, "agent_skills": skills}}
        with mock.patch.object(mcp_server.core, "api", side_effect=fake_api) as api:
            result = self.call_tool("memory_search", {
                "query": "historical practice", "kind": "practices", "method": "agentic"})["result"]
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("agentic", text)
        self.assertIn("hybrid", text)
        lines = [line for line in text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("real task" in line and "real insight" in line for line in lines))
        self.assertTrue(any("real skill" in line and "real description" in line for line in lines))
        self.assertEqual(api.call_count, 2)
        self.assertEqual([call.args[1]["method"] for call in api.call_args_list], ["agentic", "hybrid"])

    def test_read_tools_pass_valid_defaults(self):
        with mock.patch.object(mcp_server.core, "profile_text", return_value="profile") as profile:
            self.assertFalse(self.call_tool("memory_profile", {})["result"]["isError"])
            profile.assert_called_once_with(limit=8000, with_ids=True)
        with mock.patch.object(mcp_server.core, "search_text", return_value="search") as search:
            self.assertFalse(self.call_tool("memory_search", {"query": "task"})["result"]["isError"])
            search.assert_called_once_with("task", 5, "all", method="hybrid")
        with mock.patch.object(mcp_server.core, "recent_text", return_value="recent") as recent:
            self.assertFalse(self.call_tool("memory_recent", {})["result"]["isError"])
            recent.assert_called_once_with(7, 20)

    def test_write_arguments_validate_required_fields_and_nested_steps(self):
        practice = {"topic": "topic", "task": "task", "steps": [{"action": "act", "result": "ok"}],
                    "conclusion": "done"}
        invalid_practices = [dict(practice, steps=steps) for steps in (
            [], {}, [None], [{"action": "act"}], [{"action": "act", "result": 3}],
            [{"action": " ", "result": "ok"}], [{"action": "act", "result": "ok", "why": []}])]
        for args in invalid_practices:
            with self.subTest(args=args):
                self.assert_invalid_params(self.call_tool("memory_record_practice", args))
        for args in ({}, {"topic": "topic"}, {"topic": " ", "content": "x"},
                     {"topic": "topic", "content": "x", "user_said": []}):
            with self.subTest(args=args):
                self.assert_invalid_params(self.call_tool("memory_remember", args))

    def test_profile_edits_validate_conditional_fields_and_operation_count(self):
        base = {"action": "add", "type": "explicit_info", "reason": "user correction"}
        invalid = [[], [base], [dict(base, data={})], [dict(base, data=[])],
                   [dict(base, action="update", data={"description": "x"})],
                   [dict(base, action="update", item_id="item")], [dict(base, action="delete")],
                   [dict(base, action="delete", item_id=" ")], [dict(base, type="profile", data={"x": 1})],
                   [dict(base, reason="", data={"x": 1})], [dict(base, data={"x": 1})] * 51]
        for operations in invalid:
            with self.subTest(operations=operations):
                self.assert_invalid_params(self.call_tool("memory_fix_profile", {"operations": operations}))

    def test_valid_profile_edits_reach_core_without_truncation(self):
        operations = [
            {"action": "add", "type": "explicit_info", "reason": "said so", "data": {"description": "new"}},
            {"action": "update", "type": "implicit_traits", "reason": "corrected", "item_id": "item",
             "data": {"description": "current"}},
            {"action": "delete", "type": "explicit_info", "reason": "outdated", "item_id": "old"},
        ]
        with mock.patch.object(mcp_server.core, "fix_profile", return_value="applied") as fix:
            self.assertFalse(self.call_tool("memory_fix_profile", {"operations": operations})["result"]["isError"])
            fix.assert_called_once_with(operations)

    def test_write_switch_blocks_every_mutating_tool_before_core_dispatch(self):
        cases = [("memory_remember", "remember", {"topic": "t", "content": "c"}),
                 ("memory_record_practice", "record_practice", {"topic": "t", "task": "task",
                  "steps": [{"action": "a", "result": "r"}], "conclusion": "done"}),
                 ("memory_fix_profile", "fix_profile", {"operations": [{"action": "delete",
                  "type": "explicit_info", "item_id": "item", "reason": "correction"}]})]
        with mock.patch.object(mcp_server.core, "cfg", return_value={"EVEROS_WRITE": "0"}):
            for name, function, args in cases:
                with self.subTest(name=name), mock.patch.object(mcp_server.core, function) as call:
                    result = self.call_tool(name, args)["result"]
                    self.assertTrue(result["isError"])
                    self.assertIn("EVEROS_WRITE=0", result["content"][0]["text"])
                    call.assert_not_called()

    def test_execution_failures_use_tool_error_result(self):
        with mock.patch.object(mcp_server.core, "search_text", side_effect=RuntimeError("service offline")):
            response = self.call_tool("memory_search", {"query": "task"})
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("service offline", response["result"]["content"][0]["text"])

    def test_partial_practice_write_failure_is_visible_to_client(self):
        args = {"topic": "t", "task": "task", "steps": [{"action": "a", "result": "r"}],
                "conclusion": "done"}
        with mock.patch.object(mcp_server.core, "cfg", return_value={"EVEROS_USER_ID": "unit-owner",
                "EVEROS_API_VERSION": "v2", "EVEROS_READ_V1": "1"}), \
                mock.patch.object(mcp_server.core, "post_messages", side_effect=[RuntimeError("v2 down"), "accumulated"]) as post, \
                mock.patch.object(mcp_server.core, "flush", return_value="extracted"), \
                mock.patch.object(mcp_server.core.time, "sleep"):
            response = self.call_tool("memory_record_practice", args)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(post.call_count, 2)
        text = response["result"]["content"][0]["text"]
        self.assertIn("v2", text)
        self.assertIn("v1", text)


class McpStdioTests(unittest.TestCase):
    def run_stdio(self, lines):
        with tempfile.TemporaryDirectory() as temp:
            env = {key: value for key, value in os.environ.items() if not key.startswith("EVEROS_")}
            env.update({"EVEROS_HOME": temp, "EVEROS_USER_ID": "offline-test",
                        "EVEROS_API_KEY": "offline-test", "EVEROS_API_BASE": "http://127.0.0.1:1",
                        "EVEROS_WRITE": "0", "PYTHONIOENCODING": "utf-8"})
            process = subprocess.run([sys.executable, str(ROOT / "plugin" / "everos" / "mcp_server.py")],
                input="\n".join(lines) + "\n", capture_output=True, text=True, encoding="utf-8",
                env=env, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        return [json.loads(line) for line in process.stdout.splitlines()]

    def test_bad_stdio_frames_receive_errors_and_each_following_ping_survives(self):
        frames = [("{", -32700), ("null", -32600), ("[]", -32600), ("42", -32600),
                  ('"string"', -32600), ("{}", -32600),
                  ('{"jsonrpc":"2.0","method":1}', -32600),
                  ('{"jsonrpc":"2.0","id":1,"method":"ping","params":NaN}', -32700),
                  ('{"jsonrpc":"2.0","id":null,"method":"ping"}', -32600)]
        lines = []
        for index, (frame, _) in enumerate(frames):
            lines.extend([frame, json.dumps(request("ping", mid=f"after-{index}"))])
        lines += [json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                  json.dumps({"jsonrpc": "2.0", "method": "extension/notification"}),
                  json.dumps(request("ping", mid="last"))]
        responses = self.run_stdio(lines)
        self.assertEqual(len(responses), len(frames) * 2 + 1)
        for index, (_, code) in enumerate(frames):
            self.assertEqual(responses[index * 2]["error"]["code"], code)
            self.assertIsNone(responses[index * 2]["id"])
            self.assertEqual(responses[index * 2 + 1],
                             {"jsonrpc": "2.0", "id": f"after-{index}", "result": {}})
        self.assertEqual(responses[-1]["id"], "last")

    def test_readonly_stdio_returns_tool_error_and_continues(self):
        lines = [json.dumps(request("tools/call", {"name": "memory_remember",
                 "arguments": {"topic": "test", "content": "test"}})),
                 json.dumps(request("ping", mid=2))]
        responses = self.run_stdio(lines)
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("EVEROS_WRITE=0", responses[0]["result"]["content"][0]["text"])
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})


if __name__ == "__main__":
    unittest.main()
