#!/usr/bin/env python3
"""Opt-in real TraeX runtime smoke test with local model + EverOS API fixtures.

No model agent is delegated work: the model endpoint returns a fixed SSE reply.
Uses a fresh TRAE_HOME/TRAECLI_HOME/EVEROS_HOME; never loads real credentials or
edits the user's host configuration. Requires installed TraeX >= 0.209.1.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin/everos"))
from install_config import trae_hooks


class AppServer:
    def __init__(self, cli, env, stderr):
        self.proc = subprocess.Popen([cli, "app-server", "--stdio"], env=env, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
        self.queue = queue.Queue()
        self.notifications = []
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.rpc("initialize", {"clientInfo": {"name": "everos-runtime-validation", "version": "1"},
                                "capabilities": {"experimentalApi": True}})

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.queue.put(json.loads(line))
            except ValueError:
                pass

    def rpc(self, method, params, timeout=30):
        self.next_id += 1
        mid = self.next_id
        self.proc.stdin.write(json.dumps({"id": mid, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            item = self.queue.get(timeout=max(.01, deadline - time.monotonic()))
            if item.get("id") == mid:
                if "error" in item:
                    raise RuntimeError(f"{method}: {item['error']}")
                return item.get("result")
            self.notifications.append(item)

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)


def validate(cli, root, home_workspace=False):
    root = root.resolve()
    shared, runtime, everos, workspace = [root / x for x in ("shared", "runtime", "everos", "workspace")]
    if home_workspace:
        shared, runtime, workspace = root / ".trae", root / ".trae/cli", root
    for path in (shared, runtime, everos, workspace):
        path.mkdir(parents=True, exist_ok=True)
    calls, model_requests, collected = [], [], []
    condition = threading.Condition()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with condition:
                calls.append(self.path)
            if self.path.endswith("/chat/completions"):
                with condition:
                    model_requests.append(data)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in [({"role": "assistant", "content": "TRAEX_SMOKE_REPLY"}, None), ({}, "stop")]:
                    chunk = {"id": "fixture-completion", "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": "everos-fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            if self.path.endswith("/memory/get"):
                result = {"profiles": [{"profile_data": {"explicit_info": [
                    {"category": "Test", "description": "TRAEX_PROFILE_PROBE"}]}}], "total_count": 1}
            elif self.path.endswith("/memory/search"):
                result = {"episodes": [{"id": "fixture-episode", "summary": "TRAEX_RECALL_PROBE", "score": .9}]}
            elif self.path.endswith("/memory/add"):
                with condition:
                    collected.extend(data["messages"])
                    condition.notify_all()
                result = {"status": "accumulated"}
            elif self.path.endswith("/memory/flush"):
                result = {"status": "extracted"}
            else:
                self.send_error(404)
                return
            raw = json.dumps({"code": 0, "data": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    config = f'''model = "everos-fixture"
model_provider = "custom"
approval_policy = "never"
sandbox_mode = "read-only"
project_doc_max_bytes = 0

[[models]]
name = "everos-fixture"
context_window = 128000
harness_mode = "codex"
[models.open_ai]
base_url = "{base}/v1"
api_key = "fixture-only"
max_tokens = 256

[memories]
generate_memories = false
use_memories = false

[projects.{json.dumps(str(workspace))}]
trust_level = "trusted"
'''
    (shared / "traecli.toml").write_text(config)
    (everos / "config.env").write_text(f"EVEROS_API_BASE={base}\nEVEROS_API_KEY=fixture-only\n"
                                      "EVEROS_USER_ID=fixture-owner\nEVEROS_API_VERSION=v2\nEVEROS_READ_V1=0\n")
    (everos / "config.env").chmod(0o600)
    # Capture the real host payload without changing the bridge entry point.
    bridge = Path(__file__).resolve().parents[1] / "plugin/everos/hook.py"
    capture = root / "capture.py"
    capture.write_text("import json,os,subprocess,sys\nfrom pathlib import Path\n"
                       "data=sys.stdin.read()\n"
                       "with Path(os.environ['PROBE_PAYLOADS']).open('a') as f: f.write(data.strip()+'\\n')\n"
                       f"sys.exit(subprocess.run([sys.executable,{str(bridge)!r}],input=data,text=True).returncode)\n")
    data = json.loads(trae_hooks("", bridge, runtime))
    for groups in data["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                handler["command"] = handler["command"].replace(shlex.quote(str(bridge)), shlex.quote(str(capture)))
    (runtime / "hooks.json").write_text(json.dumps(data))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRAE", "EVEROS", "CODEX", "CLAUDE"))}
    env.update(TRAE_HOME=str(shared), TRAECLI_HOME=str(runtime), EVEROS_HOME=str(everos),
               PROBE_PAYLOADS=str(root / "payloads.jsonl"))
    # Disable automatic sweep/local-memory sync in this fixture only.
    mark = everos / "state/last_sweep"
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(str(time.time()))
    app = None
    try:
        with (root / "app-server.log").open("w") as log:
            app = AppServer(cli, env, log)
            listing = app.rpc("hooks/list", {"cwds": [str(workspace)]})["data"][0]
            assert not listing["errors"], listing["errors"]
            owned = list({h["key"]: h for h in listing["hooks"]
                          if str(capture) in (h.get("command") or "")}.values())
            assert len(owned) == 6, owned
            # Trust only these six freshly-created, reviewed fixture commands,
            # using the host's own hash and config API (no trust bypass flag).
            edits = [{"keyPath": "hooks.state", "value": {
                h["key"]: {"trusted_hash": h["currentHash"]} for h in owned}, "mergeStrategy": "upsert"}]
            app.rpc("config/batchWrite", {"edits": edits, "reloadUserConfig": True})
            checked = app.rpc("hooks/list", {"cwds": [str(workspace)]})["data"][0]
            assert all(h["trustStatus"] == "trusted" for h in checked["hooks"]
                       if str(capture) in (h.get("command") or "")), checked
            app.close()
            app = None
        command = [cli, "exec", "--skip-git-repo-check", "--json", "-C", str(workspace),
                   "TRAEX_SMOKE_QUESTION: validate automatic memory injection and transcript capture."]
        with (root / "events.jsonl").open("w") as out, (root / "exec.log").open("w") as err:
            proc = subprocess.run(command, env=env, stdout=out, stderr=err, timeout=60)
        assert proc.returncode == 0, f"TraeX exited {proc.returncode}; see {root / 'exec.log'}"
        assert model_requests, "Local model endpoint was not called"
        prompt = json.dumps(model_requests[0], ensure_ascii=False)
        assert "TRAEX_PROFILE_PROBE" in prompt, "SessionStart profile did not reach model"
        assert "TRAEX_RECALL_PROBE" in prompt, "UserPromptSubmit recall did not reach model"
        assert prompt.count("TRAEX_PROFILE_PROBE") == 1, "Profile injected more than once"
        assert prompt.count("TRAEX_RECALL_PROBE") == 1, "Recall injected more than once"
        with condition:
            condition.wait_for(lambda: any("TRAEX_SMOKE_REPLY" in m["content"] for m in collected), timeout=20)
        assert [m["role"] for m in collected] == ["user", "assistant"], collected
        assert collected[0]["content"].startswith("TRAEX_SMOKE_QUESTION"), collected
        assert collected[1]["content"] == "TRAEX_SMOKE_REPLY", collected
        assert "PROBE" not in json.dumps(collected), "Injected context leaked into saved dialogue"
        assert collected[0]["sender_id"] == "fixture-owner"
        assert collected[1]["sender_id"] == "fixture-owner_agent" and collected[1]["sender_name"] == "trae"
        payloads = [json.loads(line) for line in (root / "payloads.jsonl").read_text().splitlines()]
        report = {"trae_version": subprocess.check_output([cli, "--version"], text=True).strip(),
                  "profile_in_model": True, "recall_in_model": True, "saved_roles": [m["role"] for m in collected],
                  "hook_events": [p.get("hook_event_name") for p in payloads],
                  "transcript_path_present": any(p.get("transcript_path") for p in payloads),
                  "api_paths": calls, "hook_trust": "trusted (host config API)", "artifacts": str(root)}
        report["home_workspace"] = home_workspace
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return report
    finally:
        if app:
            app.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default=shutil.which("traecli") or shutil.which("traex"))
    parser.add_argument("--out", type=Path, help="Keep isolated runtime and evidence here")
    parser.add_argument("--home-workspace", action="store_true", help="Check duplicate user/project hook discovery")
    args = parser.parse_args()
    if not args.cli:
        parser.error("TraeX is not installed")
    if args.out:
        if args.out.exists():
            parser.error("--out must be a new directory")
        validate(args.cli, args.out, args.home_workspace)
    else:
        with tempfile.TemporaryDirectory(prefix="everos-traex-") as directory:
            validate(args.cli, Path(directory), args.home_workspace)
