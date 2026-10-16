#!/usr/bin/env python3
"""EverOS MCP 服务（stdio，纯标准库）：任何支持 MCP 的 agent 都能用同一套记忆。

工具：memory_profile / memory_search / memory_recent / memory_remember / memory_record_practice / memory_fix_profile。
配置同 core.py（环境变量或 ~/.everos/config.env）。stdout 只走协议消息，日志写 stderr。
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

VERSION = "0.6.0"
INSTRUCTIONS = (
    "EverOS 是用户跨 agent、跨设备共用的长期记忆。会话开始调用 memory_profile 了解用户，"
    "每条提问前都按用户的提问调用 memory_search；"
    "问「以前怎么做的」用 kind=practices。任务交付、用户纠错、偏好暴露、决策形成时调用 memory_remember"
    "（content 写结论、做了什么、证据；user_said 放用户原话）；排障或工具用法闭环时调用 "
    "memory_record_practice（至少 3 个真实步骤，没走通或出乎意料的步骤标 outcome，用户纠正原话填 user_correction）；"
    "用户纠正画像里的偏好或背景时调用 memory_fix_profile。检索结果仅供参考，与当前对话冲突时以当前为准。"
)
PRACTICE_DESCRIPTION = (
    "把一次排障或工具使用写成做法轨迹（需求 → 每步动作与结果 → 结论），EverOS 会提炼成以后可复用的经验。"
    "至少 3 个真实步骤；只有一两步的结论改用 memory_remember，不要补写没发生的步骤。"
    "EverOS v2 只提炼有试错、意外发现、非显然诊断或用户纠正的轨迹，顺利执行的线性流程通常不会生成做法 case："
    "走错的路和失败的尝试单独记一步并标 outcome=failed，出乎意料的结果标 outcome=unexpected；"
    "result 第一句写这一步的发现；这一步之后用户纠正或改向的原话填 user_correction；"
    "conclusion 写根因、解法、怎么验证、踩过的坑。写完会回读 v2 是否生成做法 case，并在返回里说明。"
)

TOOLS = [
    {"name": "memory_profile",
     "description": "读取用户画像（偏好、背景、长期项目、沟通习惯）。会话开始时调用一次。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "memory_search",
     "description": "按语义检索用户的长期记忆。kind=episodes 只查事件，practices 只查做法/经验，all 两者都查。"
                    "默认 hybrid；隐含描述或复杂历史问题可显式选 agentic 深查，默认总预算180秒，可配置或按次修改，"
                    "失败或未命中按轨道回退hybrid。",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "minLength": 1, "description": "检索语句，直接用用户的问题或主题"},
         "top_k": {"type": "integer", "default": 5,
                   "description": "每类记忆的召回上限：1–100；-1 交给服务端决定。输出仍受字数预算限制。",
                   "anyOf": [{"enum": [-1]}, {"type": "integer", "minimum": 1, "maximum": 100}]},
         "kind": {"type": "string", "enum": ["all", "episodes", "practices"], "default": "all"},
         "method": {"type": "string", "enum": ["hybrid", "agentic"], "default": "hybrid",
                    "description": "agentic 为按次深检索，允许更长等待；不会改变每轮自动检索的默认方式。"},
         "timeout": {"type": "number", "exclusiveMinimum": 0,
                     "description": "本次检索总预算（秒，含回退），优先于 EVEROS_AGENTIC_TIMEOUT；"
                                    "不填时 agentic 默认180秒或采用配置，hybrid 沿用原预算。"}}}},
    {"name": "memory_recent",
     "description": "列出最近几天的记忆标题，用来回顾近期在做什么。",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
         "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}}}},
    {"name": "memory_remember",
     "description": "写一条结构化记忆并立即入库：结论、决策及理由、纠错、项目事实、任务结果。"
                    "同一话题沿用同一个 topic。",
     "inputSchema": {"type": "object", "required": ["topic", "content"], "properties": {
         "topic": {"type": "string", "minLength": 1, "description": "话题短名，如 signin_eval_v4"},
         "content": {"type": "string", "minLength": 1, "description": "结论 + 做了什么 + 证据（文件:行、MR、commit、链接）"},
         "user_said": {"type": "string", "description": "用户原话（可选，不要改写）"}}}},
    {"name": "memory_record_practice",
     "description": PRACTICE_DESCRIPTION,
     "inputSchema": {"type": "object", "required": ["topic", "task", "steps", "conclusion"], "properties": {
         "topic": {"type": "string", "minLength": 1, "description": "话题短名；同一天同一 topic 写进同一个会话"},
         "task": {"type": "string", "minLength": 1, "description": "要解决的问题"},
         "steps": {"type": "array", "minItems": core.PRACTICE_MIN_STEPS,
                   "description": "按实际顺序的步骤，至少 3 步，包括没走通的尝试",
                   "items": {"type": "object", "required": ["action", "result"], "properties": {
             "action": {"type": "string", "minLength": 1, "description": "这一步做了什么"},
             "result": {"type": "string", "minLength": 1, "description": "第一句写这一步的发现，细节放后面"},
             "why": {"type": "string", "description": "为什么这样做、当时的判断或排除了什么（可选）"},
             "outcome": {"type": "string", "enum": list(core.PRACTICE_OUTCOMES), "default": "ok",
                         "description": "ok=按预期；failed=没走通或走错了路；unexpected=结果出乎意料"},
             "user_correction": {"type": "string",
                                 "description": "这一步之后用户纠正或改向的原话（真实发生才填，不要改写）"}}}},
         "conclusion": {"type": "string", "minLength": 1, "description": "根因、解法、怎么验证、踩过的坑"}}}},
    {"name": "memory_fix_profile",
     "description": "直接修改用户画像（v2）：用户纠正了某个偏好或背景、或画像里的进度已过期时使用。"
                    "先用 memory_profile 看现有条目，update/delete 需要 item_id。每条操作都写 reason。",
     "inputSchema": {"type": "object", "required": ["operations"], "properties": {
         "operations": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object", "required": ["action", "type", "reason"],
             "properties": {
                 "action": {"type": "string", "enum": ["add", "update", "delete"]},
                 "type": {"type": "string", "enum": ["explicit_info", "implicit_traits"]},
                 "item_id": {"type": "string", "minLength": 1, "description": "update / delete 时必填"},
                 "data": {"type": "object", "description": "add / update 时填，如 {\"category\": \"偏好\", \"description\": \"…\"}"},
                 "reason": {"type": "string", "minLength": 1, "description": "依据，如用户原话或更新的 memory_id"}}}}}}},
]

_TOOL_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
_WRITE_TOOLS = {"memory_remember", "memory_record_practice", "memory_fix_profile"}


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _finite_number(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _validate(value, schema: dict, path: str) -> None:
    """Validate the small set of JSON Schema keywords used by our six tools."""
    kind = schema.get("type")
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int,
               "number": _finite_number(value)}
    if kind in matches and not matches[kind]:
        raise RpcError(-32602, f"Invalid params: {path} must be {kind}")
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                _validate(value, option, path)
                break
            except RpcError:
                continue
        else:
            raise RpcError(-32602, f"Invalid params: {path} does not match an allowed value")
    if "enum" in schema and value not in schema["enum"]:
        raise RpcError(-32602, f"Invalid params: {path} has an unsupported value")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise RpcError(-32602, f"Invalid params: {path}.{key} is required")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                _validate(value[key], child, f"{path}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise RpcError(-32602, f"Invalid params: {path} has an invalid number of items")
        for index, item in enumerate(value):
            _validate(item, schema.get("items", {}), f"{path}[{index}]")
    elif kind == "string" and schema.get("minLength") and not value.strip():
        raise RpcError(-32602, f"Invalid params: {path} must not be blank")
    elif kind in ("integer", "number"):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise RpcError(-32602, f"Invalid params: {path} is out of range")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise RpcError(-32602, f"Invalid params: {path} is out of range")


def validate_tool(name, args) -> None:
    if not isinstance(name, str) or name not in _TOOL_SCHEMAS:
        raise RpcError(-32602, "Invalid params: unknown tool")
    if (name == "memory_record_practice" and isinstance(args, dict) and isinstance(args.get("steps"), list)
            and len(args["steps"]) < core.PRACTICE_MIN_STEPS):
        raise RpcError(-32602, "Invalid params: arguments.steps " + core.PRACTICE_STEPS_HINT)
    _validate(args, _TOOL_SCHEMAS[name], "arguments")
    if name == "memory_fix_profile":
        for index, op in enumerate(args["operations"]):
            path = f"arguments.operations[{index}]"
            if op["action"] in ("update", "delete") and not op.get("item_id", "").strip():
                raise RpcError(-32602, f"Invalid params: {path}.item_id is required")
            if op["action"] in ("add", "update") and not op.get("data"):
                raise RpcError(-32602, f"Invalid params: {path}.data must be a nonempty object")


def _valid_id(value) -> bool:
    return isinstance(value, str) or type(value) is int


def _is_response(msg: dict) -> bool:
    """Ignore valid peer responses; this server does not initiate requests."""
    if not _valid_id(msg.get("id")) or ("result" in msg) == ("error" in msg):
        return False
    if "result" in msg:
        return isinstance(msg["result"], dict)
    error = msg["error"]
    return isinstance(error, dict) and type(error.get("code")) is int and isinstance(error.get("message"), str)


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


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
        validate_tool(name, args)
        if name in _WRITE_TOOLS:
            core.ensure_writable()
        if not core.ready():
            raise RuntimeError("EverOS 未配置：设置 EVEROS_USER_ID 与 EVEROS_API_KEY，或写入 ~/.everos/config.env")
        if name == "memory_profile":
            return core.profile_text(limit=8000, with_ids=True) or "（画像为空）"
        if name == "memory_search":
            options = {"method": args.get("method", "hybrid")}
            if "timeout" in args:
                options["budget"] = args["timeout"]
            return core.search_text(args["query"], args.get("top_k", 5), args.get("kind", "all"),
                                    **options) or "（没有相关记忆）"
        if name == "memory_recent":
            return core.recent_text(args.get("days", 7), args.get("limit", 20)) or "（这段时间没有记忆）"
        if name == "memory_remember":
            return "已写入 " + core.remember(self.host, args["topic"], args["content"], args.get("user_said", ""))
        if name == "memory_record_practice":
            return "已写入 " + core.record_practice(self.host, args["topic"], args["task"], args["steps"],
                                                    args["conclusion"])
        if name == "memory_fix_profile":
            return "已修改画像 " + core.fix_profile(args["operations"])
        raise RpcError(-32602, "Invalid params: unknown tool")

    def handle(self, msg: dict) -> dict | None:
        method, params = msg["method"], msg.get("params", {})
        if method == "initialize":
            _validate(params, {"type": "object", "properties": {
                "protocolVersion": {"type": "string"}, "capabilities": {"type": "object"},
                "clientInfo": {"type": "object", "properties": {"name": {"type": "string"},
                                                                  "version": {"type": "string"}}}}}, "params")
            self.host = host_label((params.get("clientInfo") or {}).get("name") or "mcp")
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
                text = self.call(params.get("name", ""), params.get("arguments", {}))
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except RpcError:
                raise
            except Exception as exc:
                core.log(f"mcp {params.get('name')} failed: {exc!r}")
                return {"content": [{"type": "text", "text": f"EverOS 调用失败：{exc}"}], "isError": True}
        raise RpcError(-32601, f"Method not found: {method}")

    def respond(self, msg) -> dict | None:
        """Validate the MCP envelope before dispatch, keeping bad frames recoverable."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        if "method" not in msg and _is_response(msg):
            return None
        if not isinstance(msg.get("method"), str) or "result" in msg or "error" in msg:
            return _error(None, -32600, "Invalid Request")
        if "id" in msg and not _valid_id(msg["id"]):
            return _error(None, -32600, "Invalid Request")
        # MCP notifications have no ID and never receive a response. All supported
        # operations below are requests; notifications must not trigger tool writes.
        if "id" not in msg:
            return None
        mid = msg["id"]
        try:
            if "params" in msg and not isinstance(msg["params"], dict):
                raise RpcError(-32602, "Invalid params: params must be object")
            return {"jsonrpc": "2.0", "id": mid, "result": self.handle(msg)}
        except RpcError as exc:
            return _error(mid, exc.code, str(exc))
        except Exception as exc:
            core.log(f"mcp {msg['method']} failed: {exc!r}")
            return _error(mid, -32603, "Internal error")


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def main() -> None:
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            out = _error(None, -32700, "Parse error")
        else:
            out = server.respond(msg)
        if out is None:
            continue
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
