#!/usr/bin/env python3
"""EverOS MCP 服务（stdio，纯标准库）：任何支持 MCP 的 agent 都能用同一套记忆。

工具：memory_profile / memory_search / memory_recent / memory_remember / memory_record_practice。
配置同 core.py（环境变量或 ~/.everos/config.env）。stdout 只走协议消息，日志写 stderr。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

VERSION = "0.2.0"
INSTRUCTIONS = (
    "EverOS 是用户跨 agent、跨设备共用的长期记忆。会话开始调用 memory_profile 了解用户，"
    "并用首条提问调用 memory_search；用户提到「上次 / 之前 / 记得 / 按我习惯」时再检索；"
    "问「以前怎么做的」用 kind=practices。任务交付、用户纠错、偏好暴露、决策形成时调用 memory_remember"
    "（content 写结论、做了什么、证据；user_said 放用户原话）；排障或工具用法闭环时调用 "
    "memory_record_practice。检索结果仅供参考，与当前对话冲突时以当前为准。"
)

TOOLS = [
    {"name": "memory_profile",
     "description": "读取用户画像（偏好、背景、长期项目、沟通习惯）。会话开始时调用一次。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "memory_search",
     "description": "按语义检索用户的长期记忆。kind=episodes 只查事件，practices 只查做法/经验，all 两者都查。",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "检索语句，直接用用户的问题或主题"},
         "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
         "kind": {"type": "string", "enum": ["all", "episodes", "practices"], "default": "all"}}}},
    {"name": "memory_recent",
     "description": "列出最近几天的记忆标题，用来回顾近期在做什么。",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
         "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}}}},
    {"name": "memory_remember",
     "description": "写一条结构化记忆并立即入库：结论、决策及理由、纠错、项目事实、任务结果。"
                    "同一话题沿用同一个 topic。",
     "inputSchema": {"type": "object", "required": ["topic", "content"], "properties": {
         "topic": {"type": "string", "description": "话题短名，如 signin_eval_v4"},
         "content": {"type": "string", "description": "结论 + 做了什么 + 证据（文件:行、MR、commit、链接）"},
         "user_said": {"type": "string", "description": "用户原话（可选，不要改写）"}}}},
    {"name": "memory_record_practice",
     "description": "把一次排障或工具使用写成做法轨迹（需求 → 每步动作与结果 → 结论），"
                    "EverOS 会提炼成以后可复用的经验。",
     "inputSchema": {"type": "object", "required": ["topic", "task", "steps", "conclusion"], "properties": {
         "topic": {"type": "string"},
         "task": {"type": "string", "description": "要解决的问题"},
         "steps": {"type": "array", "items": {"type": "object", "required": ["action", "result"], "properties": {
             "action": {"type": "string"}, "result": {"type": "string"},
             "why": {"type": "string", "description": "为什么这样做（可选）"}}}},
         "conclusion": {"type": "string", "description": "根因、解法、怎么验证的"}}}},
]


def host_label(client: str) -> str:
    name = client.lower()
    for key, label in (("claude-code", "claude_code"), ("claude code", "claude_code"), ("codex", "codex"),
                       ("cursor", "cursor"), ("gemini", "gemini"), ("kimi", "kimi"), ("trae", "trae"),
                       ("coco", "trae"), ("claude", "claude_desktop")):
        if key in name:
            return label
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_")[:24] or "mcp"


class Server:
    def __init__(self):
        self.host = "mcp"

    def call(self, name: str, args: dict) -> str:
        if not core.ready():
            raise RuntimeError("EverOS 未配置：设置 EVEROS_USER_ID 与 EVEROS_API_KEY，或写入 ~/.everos/config.env")
        if name == "memory_profile":
            return core.profile_text(limit=8000) or "（画像为空）"
        if name == "memory_search":
            return core.search_text(args["query"], args.get("top_k", 5), args.get("kind", "all")) or "（没有相关记忆）"
        if name == "memory_recent":
            return core.recent_text(args.get("days", 7), args.get("limit", 20)) or "（这段时间没有记忆）"
        if name == "memory_remember":
            return "已写入 " + core.remember(self.host, args["topic"], args["content"], args.get("user_said", ""))
        if name == "memory_record_practice":
            return "已写入 " + core.record_practice(self.host, args["topic"], args["task"], args["steps"],
                                                    args["conclusion"])
        raise ValueError(f"未知工具 {name}")

    def handle(self, msg: dict) -> dict | None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            self.host = host_label(str((params.get("clientInfo") or {}).get("name") or "mcp"))
            return {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "everos", "version": VERSION},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "resources/list":
            return {"resources": []}
        if method == "prompts/list":
            return {"prompts": []}
        if method == "tools/call":
            try:
                text = self.call(params.get("name", ""), params.get("arguments") or {})
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as exc:
                core.log(f"mcp {params.get('name')} failed: {exc!r}")
                return {"content": [{"type": "text", "text": f"EverOS 调用失败：{exc}"}], "isError": True}
        if mid is not None:
            raise LookupError(method)
        return None


def main() -> None:
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "method" not in msg:
            continue  # 客户端对我们请求的响应，本服务不发请求
        try:
            result = server.handle(msg)
            if result is None or msg.get("id") is None:
                continue
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except LookupError as exc:
            out = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": f"Method not found: {exc}"}}
        except Exception as exc:
            out = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(exc)}}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
