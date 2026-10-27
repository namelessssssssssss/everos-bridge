"""Offline stdio tests: a blocked tool must not block the MCP control channel."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOT = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import mcp_server as m
scratch = Path(sys.argv[2])
m.MAX_PENDING_TOOL_CALLS = 2
m.core.ready = lambda: True
m.core.ensure_writable = lambda: None
def slow_search(*args, **kwargs):
    (scratch / 'started').touch()
    while not (scratch / 'release').exists():
        time.sleep(.01)
    return 'search complete'
writes = []
def remember(host, topic, content, user_said):
    writes.append(content)
    return host + ':' + ','.join(writes)
m.core.search_text = slow_search
m.core.remember = remember
m.main()
'''


class McpConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="everos-mcp-test-")
        self.scratch = Path(self.temp.name)
        env = {key: value for key, value in os.environ.items() if not key.startswith("EVEROS_")}
        env.update(EVEROS_HOME=self.temp.name, EVEROS_WRITE="0", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-c", BOOT, str(ROOT / "plugin/everos"), self.temp.name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env)
        self.responses = queue.Queue()

        def read_output():
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except ValueError as exc:
                    self.responses.put(exc)

        self.reader = threading.Thread(target=read_output, daemon=True)
        self.reader.start()
        self.addCleanup(self.close_process)
        self.send("init", "initialize", {"clientInfo": {"name": "Codex"}})
        self.assertEqual(self.receive()["id"], "init")

    def close_process(self):
        (self.scratch / "release").touch()
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.wait(timeout=5)
        self.reader.join(timeout=1)
        self.process.stdout.close()
        self.process.stderr.close()
        self.temp.cleanup()

    def send(self, mid, method, params=None):
        frame = {"jsonrpc": "2.0", "method": method}
        if mid is not None:
            frame["id"] = mid
        if params is not None:
            frame["params"] = params
        self.process.stdin.write(json.dumps(frame) + "\n")
        self.process.stdin.flush()

    def receive(self):
        try:
            frame = self.responses.get(timeout=3)
        except queue.Empty:
            self.fail("MCP response blocked while a fake tool was waiting")
        self.assertIsInstance(frame, dict, f"stdout contained a broken protocol frame: {frame!r}")
        return frame

    def start_slow_search(self):
        self.send("slow", "tools/call", {"name": "memory_search", "arguments": {"query": "offline"}})
        deadline = time.monotonic() + 3
        while not (self.scratch / "started").exists():
            if time.monotonic() >= deadline:
                self.fail("fake search did not start")
            time.sleep(.01)

    def send_write(self, mid, content):
        self.send(mid, "tools/call", {"name": "memory_remember",
                                     "arguments": {"topic": "offline", "content": content}})

    def test_ping_list_initialize_and_parse_errors_respond_before_slow_search(self):
        self.start_slow_search()
        self.send("ping", "ping")
        self.send("list", "tools/list")
        self.send("init-again", "initialize", {"clientInfo": {"name": "Kimi"}})
        self.process.stdin.write("{\n")
        self.process.stdin.flush()
        replies = [self.receive() for _ in range(4)]
        self.assertEqual([reply["id"] for reply in replies], ["ping", "list", "init-again", None])
        self.assertEqual(replies[0]["result"], {})
        self.assertIn("tools", replies[1]["result"])
        self.assertEqual(replies[-1]["error"]["code"], -32700)
        (self.scratch / "release").touch()
        self.assertEqual(self.receive()["id"], "slow")

    def test_full_queue_rejects_excess_calls_without_blocking_controls_or_executing_notifications(self):
        self.start_slow_search()
        self.send_write("first", "a")
        self.send_write("second", "b")
        self.send_write("overflow", "must-not-write")
        self.send_write(None, "notification-must-not-write")
        self.send("ping", "ping")
        rejected, pong = self.receive(), self.receive()
        self.assertEqual(rejected["id"], "overflow")
        self.assertEqual(rejected["error"]["code"], -32000)
        self.assertEqual(pong["id"], "ping")
        (self.scratch / "release").touch()
        completed = [self.receive() for _ in range(3)]
        self.assertEqual([reply["id"] for reply in completed], ["slow", "first", "second"])
        self.assertEqual(completed[-1]["result"]["content"][0]["text"], "已写入 codex:a,b")

    def test_eof_drains_accepted_tools_serially_and_preserves_submission_identity(self):
        self.start_slow_search()
        self.send_write("first", "a")
        self.send("init-again", "initialize", {"clientInfo": {"name": "Kimi"}})
        self.assertEqual(self.receive()["id"], "init-again")
        self.send_write("second", "b")
        self.process.stdin.close()
        (self.scratch / "release").touch()
        completed = [self.receive() for _ in range(3)]
        self.assertEqual([reply["id"] for reply in completed], ["slow", "first", "second"])
        self.assertEqual(completed[1]["result"]["content"][0]["text"], "已写入 codex:a")
        self.assertEqual(completed[2]["result"]["content"][0]["text"], "已写入 kimi:a,b")
        self.assertEqual(self.process.wait(timeout=3), 0)


if __name__ == "__main__":
    unittest.main()
