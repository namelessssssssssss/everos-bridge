"""EverOS 接线核心（纯标准库）：配置、读写 API、召回格式化、会话记录解析、补 flush、本地记忆同步。

被 hook.py（Claude Code / Codex 生命周期 hook）和 mcp_server.py（任意 MCP 客户端）共用。

配置优先级：环境变量 > $EVEROS_HOME/config.env（EVEROS_HOME 默认 ~/.everos）。
  EVEROS_API_KEY        必填；云端会话可改用网络密钥（代理注入 Authorization，脚本不带 key）
  EVEROS_USER_ID        记忆空间（必填，没有默认值，避免写错空间）
  EVEROS_API_BASE       默认 https://api.evermind.ai（兼容旧名 EVEROS_BASE_URL）
  EVEROS_OWNER_ALIASES  botmux <sender> 邮箱前缀命中即视为 owner，逗号分隔
  EVEROS_INJECT=0       关闭读侧注入；EVEROS_WRITE=0 关闭写侧
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
MAX_MSG_CHARS = 20000          # 只截断超长粘贴
MAX_BODY_BYTES = 250_000       # 单次 add 请求体上限（留余量）
IDLE_FLUSH_H = 2
SWEEP_EVERY_MIN = 30
READ_TIMEOUT = 6               # 读侧同步注入的网络超时，宁可不注入也不拖慢会话
AGENT_MIN_SCORE = 0.55         # 「做法」类召回的相关度门槛
RECALL_WORDS = ("上次", "之前", "以前", "我说过", "记得", "按我习惯", "老规矩", "last time",
                "remember", "previously", "as before")

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_SKIP_PREFIX = ("<local-command-", "<task-notification>", "[SYSTEM NOTIFICATION",
                "Caveat: The messages below")
# Codex 以 role=user 注入的脚手架（环境、AGENTS.md、插件推荐等），不是用户原话
_CODEX_SCAFFOLD = ("<environment_context>", "<user_instructions>", "<recommended_plugins>",
                   "<app-context>", "# AGENTS.md", "<INSTRUCTIONS>", "<user_shell_command>",
                   "<turn_aborted>", "<skill>")


def _home() -> Path:
    if os.environ.get("EVEROS_HOME"):
        return Path(os.environ["EVEROS_HOME"]).expanduser()
    return Path.home() / ".everos"


HOME = _home()
STATE = HOME / "state"
SESS_DIR = STATE / "sessions"
RECALL_DIR = STATE / "recall"
LOG = STATE / "hook.log"
SWEEP_MARK = STATE / "last_sweep"
LOCALMEM_ROOT = Path.home() / ".claude" / "projects"
LOCALMEM_LEDGER = STATE / "localmem.json"


# ---------- 基础 ----------

def log(msg: str) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.write_text("", encoding="utf-8")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(CST):%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def cfg() -> dict:
    out = {}
    path = HOME / "config.env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    for k in ("EVEROS_API_KEY", "EVEROS_USER_ID", "EVEROS_API_BASE", "EVEROS_BASE_URL",
              "EVEROS_OWNER_ALIASES", "EVEROS_INJECT", "EVEROS_WRITE"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    out.setdefault("EVEROS_API_BASE", out.get("EVEROS_BASE_URL", "https://api.evermind.ai"))
    return out


def owner() -> str:
    uid = cfg().get("EVEROS_USER_ID")
    if not uid:
        raise RuntimeError("EVEROS_USER_ID 未配置")
    return uid


def network_secret_mode() -> bool:
    """云端会话且没给 key：认为已配置网络密钥，由代理在出站时注入 Authorization。"""
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true" and not cfg().get("EVEROS_API_KEY")


def ready() -> bool:
    c = cfg()
    return bool(c.get("EVEROS_USER_ID")) and (bool(c.get("EVEROS_API_KEY")) or network_secret_mode())


def api(path: str, body: dict, timeout: int = 90) -> dict:
    c = cfg()
    headers = {"Content-Type": "application/json"}
    if c.get("EVEROS_API_KEY"):
        headers["Authorization"] = f"Bearer {c['EVEROS_API_KEY']}"
    elif not network_secret_mode():
        raise RuntimeError("EVEROS_API_KEY 未配置")
    req = urllib.request.Request(c["EVEROS_API_BASE"].rstrip("/") + path,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def ts_ms(iso: str | None) -> int:
    if not iso:
        return int(time.time() * 1000)
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def clip(text: str, limit: int = MAX_MSG_CHARS) -> str:
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[已截断，原文共 {len(text)} 字]"


def load_state(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(p: Path, st: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^\w一-鿿]+", "_", text).strip("_")[:limit]


# ---------- 读：画像与召回 ----------

def profile_text(limit: int = 4000) -> str:
    out = api("/api/v1/memories/get", {"filters": {"user_id": owner()}, "memory_type": "profile"},
              timeout=READ_TIMEOUT)
    profiles = (out.get("data") or {}).get("profiles") or []
    if not profiles:
        return ""
    pdata = profiles[0].get("profile_data") or {}
    lines = [f"- {it.get('category', '')}：{clip(it.get('description', ''), 220)}"
             for it in pdata.get("explicit_info") or []]
    lines += [f"- 特质：{clip(it.get('description', ''), 160)}" for it in pdata.get("implicit_traits") or []]
    return "\n".join(lines)[:limit]


def search_text(query: str, top_k: int = 5, kind: str = "all") -> str:
    types = {"episodes": ["episodic_memory"], "practices": ["agent_memory"]}.get(
        kind, ["episodic_memory", "agent_memory"])
    out = api("/api/v1/memories/search",
              {"filters": {"user_id": owner()}, "query": clip(query, 500), "method": "hybrid",
               "top_k": max(1, min(int(top_k), 20)), "memory_types": types}, timeout=READ_TIMEOUT * 2)
    data = out.get("data") or {}
    lines = []
    for ep in data.get("episodes") or []:
        lines.append(f"- [{str(ep.get('timestamp', ''))[:10]}] "
                     f"{clip(ep.get('summary') or ep.get('subject') or '', 260)}")
    agent = data.get("agent_memory") or {}
    for case in [c for c in agent.get("cases") or [] if float(c.get("score") or 0) >= AGENT_MIN_SCORE][:3]:
        lines.append(f"- [做法] {clip(case.get('task_intent', ''), 120)} → {clip(case.get('key_insight', ''), 180)}")
    for skill in [s for s in agent.get("skills") or [] if float(s.get("score") or 0) >= AGENT_MIN_SCORE][:3]:
        lines.append(f"- [技能] {skill.get('name', '')}：{clip(skill.get('description', ''), 180)}")
    return "\n".join(lines)


def recent_text(days: int = 7, limit: int = 20) -> str:
    cutoff = time.time() - days * 86400
    lines, page = [], 1
    while len(lines) < limit and page <= 5:
        out = api("/api/v1/memories/get", {"filters": {"user_id": owner()}, "memory_type": "episodic_memory",
                                           "page": page, "page_size": 50}, timeout=READ_TIMEOUT * 2)
        eps = (out.get("data") or {}).get("episodes") or []
        if not eps:
            break
        for ep in eps:
            if ts_ms(str(ep.get("timestamp"))) / 1000 < cutoff:
                return "\n".join(lines)
            lines.append(f"- [{str(ep.get('timestamp', ''))[:10]}] {clip(ep.get('subject') or ep.get('summary') or '', 160)}")
            if len(lines) >= limit:
                break
        page += 1
    return "\n".join(lines)


# ---------- 写：结构化记录 ----------

def post_messages(session_id: str, msgs: list[dict], agent: bool = False) -> str:
    path = "/api/v1/memories/agent" if agent else "/api/v1/memories"
    batches, cur, size = [], [], 0
    for msg in msgs:
        n = len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        if cur and (size + n > MAX_BODY_BYTES or len(cur) >= 400):
            batches.append(cur)
            cur, size = [], 0
        cur.append(msg)
        size += n
    if cur:
        batches.append(cur)
    status = ""
    for batch in batches:
        out = api(path, {"user_id": owner(), "session_id": session_id[:128], "async_mode": False,
                         "messages": batch})
        status = (out.get("data") or {}).get("status") or str(out)[:80]
    return status


def flush(session_id: str, agent: bool = False) -> str:
    path = "/api/v1/memories/agent/flush" if agent else "/api/v1/memories/flush"
    out = api(path, {"user_id": owner(), "session_id": session_id[:128]}, timeout=180)
    return (out.get("data") or {}).get("status") or str(out)[:80]


def remember(host: str, topic: str, content: str, user_said: str = "") -> str:
    """结构化记录：用户原话（可空）+ 结论/动作/证据，立即抽取。"""
    t = int(time.time() * 1000)
    sid = f"{host}_{datetime.now(CST):%Y%m%d}_{slug(topic) or 'note'}"
    msgs = [{"role": "user", "sender_id": owner(), "timestamp": t - 1,
             "content": clip(user_said or f"（{host} 记录）{topic}")},
            {"role": "assistant", "sender_id": host, "timestamp": t, "content": clip(content)}]
    add = post_messages(sid, msgs)
    time.sleep(1)
    return f"{sid}: {add}/{flush(sid)}"


def record_practice(host: str, topic: str, task: str, steps: list[dict], conclusion: str) -> str:
    """agent 轨道轨迹：需求 → 每步动作与结果 → 结论；EverOS 会提炼成可复用的做法。"""
    t = int(time.time() * 1000) - (len(steps) * 2 + 2)
    sid = f"agent_{datetime.now(CST):%Y%m%d}_{slug(topic) or 'practice'}"
    msgs = [{"role": "user", "sender_id": owner(), "timestamp": t, "content": clip(task)}]
    for i, step in enumerate(steps, 1):
        t += 1
        msgs.append({"role": "assistant", "sender_id": host, "timestamp": t,
                     "content": clip(step.get("why") or step.get("action", "")),
                     "tool_calls": [{"id": f"call_{i}", "type": "function",
                                     "function": {"name": "step", "arguments": json.dumps(
                                         {"action": step.get("action", "")}, ensure_ascii=False)}}]})
        t += 1
        msgs.append({"role": "tool", "sender_id": host, "timestamp": t, "tool_call_id": f"call_{i}",
                     "content": clip(step.get("result", ""))})
    msgs.append({"role": "assistant", "sender_id": host, "timestamp": t + 1, "content": clip(conclusion)})
    add = post_messages(sid, msgs, agent=True)
    time.sleep(1)
    return f"{sid}: {add}/{flush(sid, agent=True)}"


# ---------- 写：会话记录 → 消息 ----------

def read_new_entries(path: Path, offset: int) -> tuple[list[dict], int]:
    """从 offset 读完整行，返回 (entries, 新 offset)；不完整的末行留给下次。"""
    with open(path, "rb") as f:
        if offset > os.path.getsize(path):
            offset = 0  # 记录被重写，重读（重复内容交给 EverOS 合并）
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    entries = []
    for line in data[:end].splitlines():
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries, offset + end


def detect_host(entries: list[dict], transcript: str = "") -> str:
    if "/.codex/" in transcript or any(o.get("type") in ("session_meta", "response_item") for o in entries[:50]):
        return "codex"
    if "/.kimi-code/" in transcript or any(o.get("type") in ("turn.prompt", "context.append_message")
                                           for o in entries[:50]):
        return "kimi"
    return "claude_code"


def kimi_transcript(session_id: str) -> Path | None:
    """Kimi Code 的 hook 不带记录路径，按 session_id 找 wire.jsonl。"""
    sid = session_id.removeprefix("session_")
    base = Path(os.environ.get("KIMI_CODE_HOME") or Path.home() / ".kimi-code") / "sessions"
    hits = sorted(base.glob(f"*/session_{sid}/agents/main/wire.jsonl"))
    return hits[0] if hits else None


def _monotonic(msgs: list[dict]) -> list[dict]:
    last = 0  # EverOS 要求时间单调；同毫秒的往后挪 1ms
    for msg in msgs:
        if msg["timestamp"] <= last:
            msg["timestamp"] = last + 1
        last = msg["timestamp"]
    return msgs


def _claude_user_text(content) -> str | None:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
            return None
        text = "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    else:
        return None
    text = _REMINDER.sub("", text).strip()
    if text.startswith(("<command-name>", "<command-message>")):
        name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
        cargs = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        text = " ".join(x.group(1).strip() for x in (name, cargs) if x and x.group(1).strip())
    if not text or text.startswith(_SKIP_PREFIX):
        return None
    return text


def split_botmux(text: str) -> tuple[str, dict]:
    """botmux 转来的飞书消息：剥掉脚手架，按 <sender> 标注真实发送者。"""
    if "<user_message>" not in text:
        return text, {"sender_id": owner()}
    inner = re.search(r"<user_message>(.*?)</user_message>", text, re.S)
    body = (inner.group(1) if inner else text).strip()
    body = re.sub(r"^\[本条是话题内的回复[^\]]*\]\s*", "", body)
    who = re.search(r'<sender[^>]*?name="([^"]*)"[^>]*?email="([^"]*)"', text)
    name, email = (who.group(1), who.group(2)) if who else ("", "")
    local = email.split("@")[0]
    aliases = {a.strip() for a in cfg().get("EVEROS_OWNER_ALIASES", "").split(",") if a.strip()}
    if local and local in aliases:
        return f"（飞书经 botmux）{body}", {"sender_id": owner()}
    sid = f"feishu_{local or 'unknown'}"
    return f"（飞书经 botmux，发送者 {name or '未知'}）{body}", {"sender_id": sid, "sender_name": name or sid}


def botmux_reply(cmd: str) -> str | None:
    """botmux 会话里助手的正式回复走 `botmux send`，从命令里取出发到飞书的正文。"""
    cmd = cmd.strip()
    if not cmd.startswith("botmux send"):
        return None
    heredoc = re.search(r"<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1\s*$", cmd, re.S)
    if heredoc:
        return heredoc.group(2).strip() or None
    try:
        parts = shlex.split(cmd)[2:]
    except ValueError:
        return None
    text, skip = [], False
    for tok in parts:
        if skip:
            skip = False
        elif tok in ("--mention", "--reply-to", "--chat", "--thread", "--file"):
            skip = True
        elif not tok.startswith("--"):
            text.append(tok)
    return " ".join(text).strip() or None


def messages_claude(entries: list[dict], host: str = "claude_code") -> list[dict]:
    msgs: list[dict] = []
    ask_ids: set[str] = set()
    buf: list[str] = []
    buf_ts = 0

    def flush_assistant():
        nonlocal buf, buf_ts
        if buf:
            msgs.append({"role": "assistant", "sender_id": host, "timestamp": buf_ts,
                         "content": clip("\n\n".join(buf))})
            buf, buf_ts = [], 0

    for o in entries:
        if o.get("isSidechain") or o.get("isMeta"):
            continue
        t, m = o.get("type"), (o.get("message") or {})
        content = m.get("content")
        if t == "assistant" and isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    buf.append(c["text"].strip())
                    buf_ts = ts_ms(o.get("timestamp"))
                elif c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion":
                    ask_ids.add(c.get("id", ""))
                elif c.get("type") == "tool_use" and c.get("name") == "Bash":
                    reply = botmux_reply(str((c.get("input") or {}).get("command", "")))
                    if reply:
                        buf.append(f"（经 botmux 发到飞书）{reply}")
                        buf_ts = ts_ms(o.get("timestamp"))
        elif t == "user":
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in ask_ids:
                        r = c.get("content")
                        if isinstance(r, list):
                            r = "\n".join(x.get("text", "") for x in r if isinstance(x, dict))
                        flush_assistant()
                        msgs.append({"role": "user", "sender_id": owner(), "timestamp": ts_ms(o.get("timestamp")),
                                     "content": clip(f"（回答选择题）{r}")})
            text = _claude_user_text(content)
            if text:
                flush_assistant()
                text, sender = split_botmux(text)
                msgs.append({"role": "user", **sender, "timestamp": ts_ms(o.get("timestamp")), "content": clip(text)})
    flush_assistant()
    return _monotonic(msgs)


def messages_codex(entries: list[dict], host: str = "codex") -> list[dict]:
    """Codex rollout：response_item 里 role=user 的原话（剔除脚手架）与 role=assistant 的输出。"""
    msgs: list[dict] = []
    for o in entries:
        p = o.get("payload") or {}
        if o.get("type") != "response_item" or p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
            continue
        parts = [c.get("text", "") for c in p.get("content") or []
                 if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")]
        text = "\n".join(x for x in parts if x).strip()
        if not text:
            continue
        if p["role"] == "user":
            if text.startswith(_CODEX_SCAFFOLD):
                continue
            text, sender = split_botmux(text)
            msgs.append({"role": "user", **sender, "timestamp": ts_ms(o.get("timestamp")), "content": clip(text)})
        elif msgs and msgs[-1]["role"] == "assistant":
            msgs[-1]["content"] = clip(msgs[-1]["content"] + "\n\n" + text)
            msgs[-1]["timestamp"] = ts_ms(o.get("timestamp"))
        else:
            msgs.append({"role": "assistant", "sender_id": host, "timestamp": ts_ms(o.get("timestamp")),
                         "content": clip(text)})
    return _monotonic(msgs)


def messages_kimi(entries: list[dict], host: str = "kimi") -> list[dict]:
    """Kimi Code wire.jsonl：用户原话在 context.append_message（origin=user），回复在 content.part（text）。"""
    msgs: list[dict] = []
    for o in entries:
        t = o.get("type")
        if t == "context.append_message":
            m = o.get("message") or {}
            if m.get("role") != "user" or (m.get("origin") or {}).get("kind") != "user":
                continue
            text = "\n".join(c.get("text", "") for c in m.get("content") or [] if isinstance(c, dict)
                             and c.get("type") == "text").strip()
            if text:
                text, sender = split_botmux(text)
                msgs.append({"role": "user", **sender, "timestamp": int(o.get("time") or 0) or ts_ms(None),
                             "content": clip(text)})
        elif t == "context.append_loop_event":
            ev = o.get("event") or {}
            part = ev.get("part") or {}
            if ev.get("type") != "content.part" or part.get("type") != "text" or not str(part.get("text", "")).strip():
                continue
            stamp = int(o.get("time") or 0) or ts_ms(None)
            if msgs and msgs[-1]["role"] == "assistant":
                sep = "" if msgs[-1].get("_step") == ev.get("stepUuid") else "\n\n"  # 跨步骤（中间有工具调用）分段
                msgs[-1]["content"] = clip(msgs[-1]["content"] + sep + str(part["text"]))
                msgs[-1]["timestamp"] = stamp
                msgs[-1]["_step"] = ev.get("stepUuid")
            else:
                msgs.append({"role": "assistant", "sender_id": host, "timestamp": stamp,
                             "content": clip(str(part["text"])), "_step": ev.get("stepUuid")})
    for m in msgs:
        m.pop("_step", None)
    return _monotonic(msgs)


def session_name(entries: list[dict], host: str, cwd: str, sid: str, title: str = "") -> str:
    first_ts = None
    for o in entries:
        if o.get("type") == "custom-title" and o.get("customTitle"):
            title = o["customTitle"]
        if first_ts is None and o.get("timestamp"):
            first_ts = o["timestamp"]
    day = (datetime.fromisoformat(first_ts.replace("Z", "+00:00")).astimezone(CST) if first_ts
           else datetime.now(CST))
    prefix = {"claude_code": "cc", "codex": "cx", "kimi": "km"}.get(host, slug(host, 12) or "agent")
    return f"{prefix}_{day:%Y%m%d}_{slug(title or Path(cwd or 'x').name) or 'session'}_{sid[:8]}"


def write_turns(payload: dict) -> None:
    sid = str(payload.get("session_id") or "")
    tpath = Path(str(payload.get("transcript_path") or ""))
    event = str(payload.get("hook_event_name") or "")
    if not tpath.is_file() and str(payload.get("client_type", "")).startswith("kimi") and sid:
        tpath = kimi_transcript(sid) or tpath
    if not sid or not tpath.is_file():
        return
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    sp = SESS_DIR / f"{sid}.json"
    with open(sp.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)  # 同一会话的 Stop / SessionEnd 串行
        st = load_state(sp)
        offset = int(st.get("offset", 0))
        entries, new_offset = read_new_entries(tpath, offset)
        if not st.get("everos_session_id"):
            head, _ = read_new_entries(tpath, 0) if offset else (entries, 0)
            host = detect_host(head, str(tpath))
            st.update(claude_session_id=sid, host=host, cwd=str(payload.get("cwd") or ""), transcript_path=str(tpath),
                      everos_session_id=session_name(head, host, str(payload.get("cwd") or ""),
                                                     sid.removeprefix("session_"), str(payload.get("session_title") or "")))
        host = st.get("host") or "claude_code"
        parse = {"codex": messages_codex, "kimi": messages_kimi}.get(host, messages_claude)
        msgs = parse(entries, host)
        if msgs:
            res = post_messages(st["everos_session_id"], msgs)
            st.update(pending=True, last_write_ms=int(time.time() * 1000), last_write_status=res,
                      writes=st.get("writes", 0) + 1, messages=st.get("messages", 0) + len(msgs))
            log(f"{event} add {st['everos_session_id']} +{len(msgs)} -> {res}")
        st["offset"] = new_offset
        save_state(sp, st)
        if event in ("SessionEnd", "PreCompact") and st.get("pending"):
            try:
                res = flush(st["everos_session_id"])
                st.update(pending=False, last_flush_ms=int(time.time() * 1000), last_flush_status=res)
                save_state(sp, st)
                log(f"{event} flush {st['everos_session_id']} -> {res}")
            except Exception as exc:  # 留给 sweep 补
                log(f"{event} flush failed {st['everos_session_id']}: {exc!r}")
    sweep()


# ---------- 兜底：补 flush 与本地记忆同步 ----------

def _frontmatter(text: str) -> tuple[dict, str]:
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        for key in ("name", "description", "type"):
            hit = re.search(rf"^\s*{key}:\s*(.+)$", m.group(1), re.M)
            if hit:
                meta[key] = hit.group(1).strip()
    return meta, body.strip()


def sync_local_memory(limit: int | None = None) -> None:
    """Claude Code 本地记忆（~/.claude/projects/*/memory/*.md）新增或改动后同步进 EverOS。"""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "localmem.lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        ledger = load_state(LOCALMEM_LEDGER)
        done = fails = 0
        for f in sorted(LOCALMEM_ROOT.glob("*/memory/*.md"), key=lambda f: f.stat().st_mtime):
            if f.name == "MEMORY.md":
                continue
            key, mtime = str(f), f.stat().st_mtime
            prev = ledger.get(key, {})
            if prev.get("mtime", 0) >= mtime or prev.get("attempts", 0) >= 3:
                continue
            if limit is not None and done >= limit:
                break
            meta, body = _frontmatter(f.read_text(encoding="utf-8"))
            project = re.sub(r"^-Users-[^-]+-(GolandProjects-)?", "", f.parent.parent.name).replace("-", "_")
            sid = f"cc_localmem_{project}_{meta.get('name') or f.stem}"[:128]
            # 时间戳用同步时刻：约 2 个月前的旧时间戳会被 EverOS 静默丢弃（accumulated 但不抽取）
            t = int(time.time() * 1000)
            born = datetime.fromtimestamp(getattr(f.stat(), "st_birthtime", mtime), CST)
            dates = f"首次记录 {born:%Y-%m-%d}，最后更新 {datetime.fromtimestamp(mtime, CST):%Y-%m-%d}"
            msgs = [{"role": "user", "sender_id": owner(), "timestamp": t - 1,
                     "content": f"（本地 Claude 记忆 · {meta.get('type', '?')} · {project} · {dates}）"
                                f"{meta.get('description', f.stem)}"},
                    {"role": "assistant", "sender_id": "claude_code", "timestamp": t, "content": clip(body)}]
            try:
                add = post_messages(sid, msgs)
                time.sleep(1)
                res = flush(sid)
                ok = res == "extracted"
                ledger[key] = {"mtime": mtime if ok else 0, "session": sid, "add": add, "flush": res,
                               "attempts": 0 if ok else prev.get("attempts", 0) + 1,
                               "synced_at": datetime.now(CST).isoformat(timespec="seconds")}
                save_state(LOCALMEM_LEDGER, ledger)
                log(f"localmem {sid} -> {add}/{res}")
                done += 1
            except Exception as exc:  # 单条超时跳过；连续 3 次失败说明网络或鉴权有问题，停手
                log(f"localmem failed {sid}: {exc!r}")
                fails += 1
                if fails >= 3:
                    break
                continue
            fails = 0


def sweep(force: bool = False) -> None:
    """补 flush 闲置会话并同步本地记忆；限频，避免每轮都扫。"""
    now = time.time()
    STATE.mkdir(parents=True, exist_ok=True)
    if not force and SWEEP_MARK.exists() and now - SWEEP_MARK.stat().st_mtime < SWEEP_EVERY_MIN * 60:
        return
    SWEEP_MARK.touch()
    for p in sorted(SESS_DIR.glob("*.json")):
        with open(p.with_suffix(".lock"), "w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            st = load_state(p)
            idle_h = (now * 1000 - st.get("last_write_ms", 0)) / 3_600_000
            if st.get("pending") and (force or idle_h >= IDLE_FLUSH_H):
                try:
                    res = flush(st["everos_session_id"])
                    st.update(pending=False, last_flush_ms=int(now * 1000), last_flush_status=res)
                    save_state(p, st)
                    log(f"sweep flush {st['everos_session_id']} -> {res}")
                except Exception as exc:
                    log(f"sweep flush failed {st.get('everos_session_id')}: {exc!r}")
    sync_local_memory(limit=None if force else 10)


def doctor() -> str:
    c = cfg()
    lines = [f"EVEROS_HOME   {HOME}（config.env {'存在' if (HOME / 'config.env').is_file() else '无'}）",
             f"API base      {c['EVEROS_API_BASE']}",
             f"user_id       {c.get('EVEROS_USER_ID') or '缺失：设置 EVEROS_USER_ID'}",
             "API key       " + ("已配置" if c.get("EVEROS_API_KEY") else
                                  "未配置，云端走网络密钥" if network_secret_mode() else
                                  "缺失：设置 EVEROS_API_KEY 或写入 config.env"),
             f"读侧注入      {'关' if c.get('EVEROS_INJECT') == '0' else '开'}；写侧 {'关' if c.get('EVEROS_WRITE') == '0' else '开'}"]
    try:
        t = time.time()
        out = api("/api/v1/memories/get", {"filters": {"user_id": owner()}, "memory_type": "episodic_memory",
                                           "page": 1, "page_size": 1}, timeout=15)
        lines.append(f"连通性        OK，episode 共 {(out.get('data') or {}).get('total_count')} 条，耗时 {time.time() - t:.1f}s")
    except Exception as exc:
        lines.append(f"连通性        失败：{exc!r}（云端环境需放行 api.evermind.ai）")
    lines.append(f"状态目录      {STATE}（{len(list(SESS_DIR.glob('*.json')))} 个会话账本）")
    return "\n".join(lines)
