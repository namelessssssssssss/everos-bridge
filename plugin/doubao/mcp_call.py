#!/usr/bin/env python3
"""Call EverOS MCP tools from a host that cannot register stdio MCP servers.

Examples:
  everos-mcp list
  everos-mcp memory_search '{"query":"...","kind":"all"}'
  everos-mcp memory_remember @/abs/path/args.json

Speaks JSON-RPC 2025-06-18 to the unmodified plugin/everos/mcp_server.py, so tool schemas,
parameter validation and write semantics are identical to hosts with native MCP support.
Credentials are read by the server from the environment / $EVEROS_HOME/config.env; this
client never handles secrets.

Exit codes: 0 ok, 1 tool/RPC error, 2 usage error.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "plugin" / "everos" / "mcp_server.py"
TIMEOUT = float(os.environ.get("EVEROS_MCP_CLIENT_TIMEOUT", "200"))  # > agentic 180s budget + margin


def call(tool: str, args: dict, server: Path = SERVER, timeout: float = TIMEOUT) -> int:
    env = {**os.environ, "EVEROS_HOST": os.environ.get("EVEROS_HOST", "doubao_work")}
    proc = subprocess.Popen([sys.executable, str(server)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "doubao_work", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"} if tool == "list" else
         {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}),
    ]
    try:
        out, _ = proc.communicate("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reqs), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        print(json.dumps({"error": f"MCP 调用超时（{timeout}s）"}, ensure_ascii=False), file=sys.stderr)
        return 1
    result = None
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == 2:
            result = msg
    if result is None:
        print(json.dumps({"error": "MCP server 无响应"}, ensure_ascii=False), file=sys.stderr)
        return 1
    if "error" in result:
        print(json.dumps({"error": result["error"]}, ensure_ascii=False), file=sys.stderr)
        return 1
    payload = result["result"]
    if tool == "list":
        for item in payload.get("tools", []):
            print(f"{item['name']}\t{item['description'].splitlines()[0][:90]}")
        return 0
    for content in payload.get("content", []):
        if content.get("type") == "text":
            print(content.get("text", ""))
    return 1 if payload.get("isError") else 0


def main(argv: list[str]) -> int:
    if not SERVER.is_file():
        print(json.dumps({"error": f"MCP server not found: {SERVER}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    tool = argv[0]
    args: dict = {}
    if len(argv) > 1:
        raw = argv[1]
        if raw.startswith("@"):
            raw = Path(raw[1:]).read_text(encoding="utf-8")
        try:
            args = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(json.dumps({"error": f"args 不是合法 JSON: {exc}"}, ensure_ascii=False), file=sys.stderr)
            return 2
    return call(tool, args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(0)
