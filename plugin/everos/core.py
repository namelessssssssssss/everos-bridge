"""EverOS 接线核心（纯标准库）：配置、读写 API、召回格式化、会话记录解析、补 flush、本地记忆同步。

被 hook.py（Claude Code / Codex 生命周期 hook）和 mcp_server.py（任意 MCP 客户端）共用。

配置优先级：环境变量 > $EVEROS_HOME/config.env（EVEROS_HOME 默认 ~/.everos）。
  EVEROS_API_KEY        必填；云端会话可改用网络密钥（代理注入 Authorization，脚本不带 key）
  EVEROS_USER_ID        记忆空间（必填，没有默认值，避免写错空间）
  EVEROS_API_BASE       默认 https://api.evermind.ai（兼容旧名 EVEROS_BASE_URL）
  EVEROS_OWNER_ALIASES  botmux <sender> 邮箱前缀命中即视为 owner，逗号分隔
  EVEROS_INJECT=0       关闭读侧注入；EVEROS_WRITE=0 关闭写侧
  EVEROS_API_VERSION    v2（默认）或 v1；v1 即将退役，只为回退保留
  EVEROS_READ_V1=1      过渡期：读取时同时查 v1 旧库并合并（官方迁完数据后关掉）
  EVEROS_AGENT_ID       v2 里助手消息的归属 agent，默认 <user_id>_agent；原 agent 名记在 sender_name
  EVEROS_MIN_SCORE      v2 检索的得分门槛，默认 0.3
  EVEROS_RECALL_TIMEOUT 旧版画像读取预算（秒）；不再控制提问检索
  EVEROS_AGENTIC_TIMEOUT 自动/手动 agentic 检索的总预算（秒），默认 180，包含回退
  EVEROS_RECALL_CHARS   每轮注入上限（字），默认 2000
  EVEROS_CASE_WAIT      写做法后等待 v2 生成做法 case 的上限（秒），默认 30；0 关闭回读
"""
from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import math
import os
import queue
import re
import shlex
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import trae

CST = timezone(timedelta(hours=8))
MAX_MSG_CHARS = 20000          # 只截断超长粘贴
MAX_BODY_BYTES = 250_000       # 单次 add 请求体上限（留余量）
IDLE_FLUSH_H = 2
SWEEP_EVERY_MIN = 30
READ_TIMEOUT = 6               # 读侧同步注入的网络超时，宁可不注入也不拖慢会话
AGENTIC_TIMEOUT = 180          # 自动/手动深检索的默认共同预算，包含回退
FALLBACK_TIMEOUT = 30          # 为正常但较慢的 hybrid 请求保留恢复窗口
AGENT_MIN_SCORE = 0.55         # 「做法」类召回的相关度门槛
PRACTICE_MIN_STEPS = 3         # v2 少于 3 轮工具调用不提炼做法 case
CASE_WAIT = 30                 # 写做法后等待 v2 做法 case 的默认上限（秒）
CASE_WAIT_MAX = 150            # 留在 Codex / MiMo 默认 190 秒外层超时之内
CASE_POLL = 3
# 步骤结果标记：(工具结果前缀, 复盘句开头)。v2 只提炼有试错、意外或用户纠正的轨迹。
PRACTICE_OUTCOMES = {"ok": None, "failed": ("失败", "这一步没走通"),
                     "unexpected": ("意外", "这一步的结果出乎意料")}
PRACTICE_STEPS_HINT = ("做法轨迹至少需要 3 个真实步骤：EverOS v2 少于 3 轮工具调用不会提炼做法 case。"
                       "只有一两步的结论改用 memory_remember；不要补写没有发生的步骤。")
RECALL_WORDS = ("上次", "之前", "以前", "我说过", "记得", "按我习惯", "老规矩", "last time",
                "remember", "previously", "as before")

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# Kimi Work 桌面端给每条用户提问加的前导脚手架（不是用户原话）
_DESKTOP_META = re.compile(r"^<meta\b[^>]*?/?>\s*", re.S)
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
        with open(LOG.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if LOG.exists() and LOG.stat().st_size > 1_000_000:
                try:
                    LOG.replace(LOG.with_suffix(".log.1"))
                except OSError:
                    # Windows readers can deny rename/delete sharing. Keep the
                    # current event and retry rotation on a later append.
                    pass
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
              "EVEROS_OWNER_ALIASES", "EVEROS_INJECT", "EVEROS_WRITE", "EVEROS_API_VERSION",
              "EVEROS_READ_V1", "EVEROS_AGENT_ID", "EVEROS_MIN_SCORE", "EVEROS_RECALL_TIMEOUT",
              "EVEROS_RECALL_CHARS", "EVEROS_AGENTIC_TIMEOUT", "EVEROS_CASE_WAIT"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    out.setdefault("EVEROS_API_BASE", out.get("EVEROS_BASE_URL", "https://api.evermind.ai"))
    return out


def owner() -> str:
    uid = cfg().get("EVEROS_USER_ID")
    if not uid:
        raise RuntimeError("EVEROS_USER_ID 未配置")
    return uid


def version() -> str:
    return "v1" if cfg().get("EVEROS_API_VERSION", "v2").lower() == "v1" else "v2"


def read_v1() -> bool:
    return version() == "v1" or cfg().get("EVEROS_READ_V1") == "1"


def agent_id() -> str:
    return cfg().get("EVEROS_AGENT_ID") or f"{owner()}_agent"


def _num(key: str, default: float) -> float:
    try:
        return float(cfg().get(key, default))
    except ValueError:
        return default


def agentic_timeout(config: dict | None = None) -> float:
    """Validate the shared automatic/manual deep-search budget."""
    value = (cfg() if config is None else config).get("EVEROS_AGENTIC_TIMEOUT", AGENTIC_TIMEOUT)
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("EVEROS_AGENTIC_TIMEOUT 须为正有限秒数") from None
    if isinstance(value, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("EVEROS_AGENTIC_TIMEOUT 须为正有限秒数")
    return seconds


def case_wait() -> float:
    """写做法后的回读上限；配置非法时退回默认值，不因此让写入失败。"""
    value = cfg().get("EVEROS_CASE_WAIT", CASE_WAIT)
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(CASE_WAIT)
    if not math.isfinite(seconds) or seconds < 0:
        return float(CASE_WAIT)
    return min(seconds, float(CASE_WAIT_MAX))


def network_secret_mode() -> bool:
    """云端会话且没给 key：认为已配置网络密钥，由代理在出站时注入 Authorization。"""
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true" and not cfg().get("EVEROS_API_KEY")


def ready() -> bool:
    c = cfg()
    return bool(c.get("EVEROS_USER_ID")) and (bool(c.get("EVEROS_API_KEY")) or network_secret_mode())


def ensure_writable() -> None:
    """所有写入口共用开关，包括 MCP 与手工维护命令。"""
    if cfg().get("EVEROS_WRITE") == "0":
        raise RuntimeError("EverOS 写入已关闭（EVEROS_WRITE=0）")


class APIResponseError(RuntimeError):
    """服务端返回业务失败或未完成；与可重试的网络异常区分。"""


def _check_response(out: dict) -> dict:
    if not isinstance(out, dict):
        raise APIResponseError("EverOS 返回了无效响应对象")
    code = out.get("code")
    if (code is not None and code not in (0, 200, "0", "200")) or out.get("success") is False or out.get("error"):
        detail = str(out.get("message") or out.get("error") or "业务请求失败")[:200]
        raise APIResponseError(f"EverOS 请求失败（code={code!r}）：{detail}")
    if str(out.get("status", "")).lower() in ("error", "failed", "failure"):
        raise APIResponseError("EverOS 请求失败：" + str(out.get("message") or out["status"])[:200])
    return out


def api(path: str, body: dict, timeout: float = 90) -> dict:
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
    return _check_response(json.loads(raw) if raw else {})


def ts_ms(iso: str | None) -> int:
    if not iso:
        return int(time.time() * 1000)
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def epoch_ms(value) -> int:
    """接口时间戳统一成毫秒：兼容秒/毫秒数字和 ISO 字符串；无时区按 UTC，解析失败返回 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return 0
        return int(round(value if value > 1e11 else value * 1000))
    text = str(value or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return epoch_ms(float(text))
    text = re.sub(r"(\.\d{1,6})\d*", lambda m: m.group(1).ljust(7, "0"), text.replace("Z", "+00:00"), count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


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


# ---------- 读：画像与召回（v2 为主，过渡期合并 v1） ----------

def _profile_lines(pdata: dict, with_ids: bool = False) -> list[str]:
    def tag(it):
        return f"（{it.get('item_id')}）" if with_ids and it.get("item_id") else ""
    lines = [f"- {it.get('category', '')}{tag(it)}：{clip(it.get('description', ''), 220)}"
             for it in pdata.get("explicit_info") or []]
    lines += [f"- 特质{tag(it)}：{clip(it.get('description', ''), 160)}" for it in pdata.get("implicit_traits") or []]
    return lines


def _read_jobs(jobs: dict, budget: float, *, operation: str | None = None) -> dict:
    """只等到共同 deadline；daemon 线程不会在 hook 退出时再等待网络。"""
    deadline = time.monotonic() + max(0.0, budget)
    results, completed = ReadResults(), queue.Queue()
    started = time.monotonic()
    operation = operation or uuid.uuid4().hex[:16]

    def run(name, path, body):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        for attempt in (1, 2):
            request_started = time.monotonic()
            remaining = deadline - request_started
            if remaining <= 0:
                return
            try:
                data = _check_response(api(path, body, timeout=remaining)).get("data")
                data = {} if data is None else data
                validate_read_data(data)
                status = "success" if time.monotonic() <= deadline else "late_result"
                log_read(operation, name, body, attempt, request_started, status)
                completed.put((name, data, None))
                return
            except Exception as exc:
                failure = read_failure(exc, body)
                log_read(operation, name, body, attempt, request_started, "error", failure)
                # Only retry safe reads with a transient transport/server error. A
                # HTTP 422 needs its error detail investigated, not a blind retry.
                remaining = deadline - time.monotonic()
                delay = failure.get("retry_after", .2)
                if attempt == 1 and failure["retryable"] and remaining > max(.5, delay):
                    time.sleep(delay)
                    continue
                completed.put((name, None, failure))
                return

    if budget <= 0:
        return results
    for name, (path, body) in jobs.items():
        threading.Thread(target=run, args=(name, path, body), daemon=True).start()
    for _ in jobs:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            name, data, error = completed.get(timeout=remaining)
        except queue.Empty:
            break
        if error is not None:
            results.failures[name] = error
        else:
            results[name] = data
    for name in jobs.keys() - results.keys() - results.failures.keys():
        results.failures[name] = {"kind": "deadline", "retryable": True}
        log_read(operation, name, jobs[name][1], 0, started, "deadline")
    return results


def validate_read_data(data):
    if not isinstance(data, dict):
        raise APIResponseError("EverOS 返回了无效的 data 对象")
    for key in ("episodes", "profiles", "agent_cases", "agent_skills", "cases", "skills"):
        rows = data.get(key)
        if rows is not None and (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)):
            raise APIResponseError("EverOS 返回了无效的 " + key + " 列表")
    if "agent_memory" in data and data["agent_memory"] is not None:
        validate_read_data(data["agent_memory"])


class ReadResults(dict):
    """Keep successful empty reads distinct from failed or unfinished tracks."""
    def __init__(self):
        super().__init__()
        self.failures = {}


_READ_ERROR_CODES = frozenset({
    "InvalidParameter", "PROVIDER_NOT_CONFIGURED", "INVALID_INPUT",
    "invalid_request", "invalid_request_error", "invalid_parameter", "validation_error",
    "rate_limit_exceeded", "internal_error", "internal_server_error", "service_unavailable",
    "timeout", "retrieval_error",
})
_READ_ERROR_TYPES = frozenset({
    "missing", "value_error", "type_error", "string_type", "string_too_short", "string_too_long",
    "string_pattern_mismatch", "int_type", "int_parsing", "float_type", "float_parsing",
    "bool_type", "bool_parsing", "list_type", "dict_type", "model_type", "model_attributes_type",
    "enum", "literal_error", "greater_than", "greater_than_equal", "less_than", "less_than_equal",
    "finite_number", "extra_forbidden", "json_invalid",
})
_READ_ERROR_PARAMS = frozenset({
    "query", "user_id", "agent_id", "method", "top_k", "include_profile", "min_score",
    "memory_type", "memory_types", "filters", "page_size", "page", "limit", "sort_by",
    "sort_order", "enable_llm_rerank",
})
_READ_ERROR_LOCATIONS = _READ_ERROR_PARAMS | {"body", "path", "header", "__root__"}


def _read_error_location(value):
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 8:
        return None
    location = []
    for part in value:
        if isinstance(part, str) and part in _READ_ERROR_LOCATIONS:
            location.append(part)
        elif type(part) is int and part >= 0:
            location.append("[index]")
        else:
            return None
    return location


def _read_error_projection(raw):
    """Only fixed diagnostic symbols survive; free text may echo private input."""
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("error"), dict):
        raw = raw["error"]
    projected = {}
    code = raw.get("code")
    if ((isinstance(code, str) and code in _READ_ERROR_CODES)
            or (type(code) is int and code in (400, 401, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504))):
        projected["code"] = code
    if isinstance(raw.get("type"), str) and raw["type"] in _READ_ERROR_TYPES:
        projected["type"] = raw["type"]
    if isinstance(raw.get("param"), str) and raw["param"] in _READ_ERROR_PARAMS:
        projected["param"] = raw["param"]
    location = _read_error_location(raw.get("loc"))
    if location is not None:
        projected["loc"] = location
    if isinstance(raw.get("detail"), list):
        details = []
        for row in raw["detail"][:4]:
            if not isinstance(row, dict):
                continue
            item = {}
            if isinstance(row.get("type"), str) and row["type"] in _READ_ERROR_TYPES:
                item["type"] = row["type"]
            location = _read_error_location(row.get("loc"))
            if location is not None:
                item["loc"] = location
            if item:
                details.append(item)
        if details:
            projected["detail"] = details
    return projected


def read_failure(exc: Exception, body: dict) -> dict:
    """Log allowlisted diagnostic symbols, never service messages or echoed input."""
    failure = {"kind": "error", "exception": type(exc).__name__, "retryable": False}
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(exc, urllib.error.HTTPError):
        failure.update(kind="http", status=exc.code,
                       retryable=exc.code in (408, 429, 500, 502, 503, 504))
        try:
            raw = json.loads(exc.read(16384).decode("utf-8", "replace"))
        except Exception:
            raw = {}
        finally:
            try:
                exc.close()
            except Exception:
                pass
        failure.update(_read_error_projection(raw))
        if exc.headers:
            try:
                delay = float(exc.headers.get("Retry-After", "0"))
                if math.isfinite(delay) and delay > 0:
                    failure["retry_after"] = delay
            except ValueError:
                pass
            request_id = exc.headers.get("x-request-id") or exc.headers.get("x-correlation-id")
            if isinstance(request_id, str) and request_id:
                failure["request_id_sha256"] = hashlib.sha256(request_id.encode("utf-8", "replace")).hexdigest()
    elif isinstance(reason, (TimeoutError, socket.timeout)):
        failure.update(kind="timeout", retryable=True)
    elif isinstance(exc, (urllib.error.URLError, ConnectionError)):
        failure.update(kind="network", retryable=True)
    elif isinstance(reason, http.client.IncompleteRead):
        failure.update(kind="network", retryable=True)
    return failure


def log_read(operation, name, body, attempt, started, status, failure=None):
    event = {"event": "memory_read", "operation": operation, "track": name,
             "method": body.get("method", "get"), "attempt": attempt, "status": status,
             "elapsed_ms": round((time.monotonic() - started) * 1000)}
    if failure:
        event["failure"] = failure
    log(json.dumps(event, ensure_ascii=False, separators=(",", ":")))


def failure_label(failure):
    kind = failure.get("kind")
    if kind == "http":
        return "HTTP " + str(failure["status"])
    return {"deadline": "超过检索时限", "timeout": "网络超时", "network": "网络连接失败"}.get(kind, "请求失败")


def _agentic_jobs(jobs, deadline, reserve, notices):
    """Each track can recover immediately, independently of slower siblings."""
    primary_deadline = deadline - reserve
    completed = queue.Queue()
    results, methods = ReadResults(), {}
    operation = uuid.uuid4().hex[:16]
    labels = {"v2u": "用户记忆", "v2a": "做法记忆"}

    def run(name, path, body):
        final, method, note = ReadResults(), body["method"], ""
        try:
            primary = _read_jobs({name: (path, body)}, max(0.0, primary_deadline - time.monotonic()), operation=operation)
            final = primary
            if name in labels and (name not in primary or not _has_search_hits(name, primary[name])):
                reason = "未命中" if name in primary else failure_label(getattr(primary, "failures", {}).get(name, {}))
                hybrid = dict(body, method="hybrid")
                if name == "v2u":
                    hybrid["min_score"] = _num("EVEROS_MIN_SCORE", .3)
                recovered = _read_jobs({name: (path, hybrid)}, max(0.0, min(reserve, deadline - time.monotonic())), operation=operation)
                if name in recovered:
                    final, method = recovered, "hybrid"
                    note = f"（{labels[name]}的 agentic {reason}，已回退 hybrid。）"
                else:
                    failure = getattr(recovered, "failures", {}).get(name, {"kind": "deadline"})
                    note = f"（{labels[name]}的 agentic {reason}，hybrid 回退{failure_label(failure)}。）"
        except Exception as exc:
            final = ReadResults()
            final.failures[name] = read_failure(exc, body)
            note = f"（{labels.get(name, name)}检索{failure_label(final.failures[name])}。）"
        finally:
            completed.put((name, final, method, note))

    for name, (path, body) in jobs.items():
        threading.Thread(target=run, args=(name, path, body), daemon=True).start()
    pending = set(jobs)
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            name, final, method, note = completed.get(timeout=remaining)
        except queue.Empty:
            break
        pending.remove(name)
        if name in final:
            results[name], methods[name] = final[name], method
        else:
            results.failures[name] = getattr(final, "failures", {}).get(name, {"kind": "deadline"})
        if note:
            notices.append(note)
    for name in pending:
        results.failures[name] = {"kind": "deadline", "retryable": True}
        if name in labels:
            notices.append(f"（{labels[name]}的 agentic / hybrid 检索超过总时限。）")
    return results, methods


def profile_text(limit: int = 4000, with_ids: bool = False, budget: float | None = None) -> str:
    lines, seen = [], set()
    sources = []
    if version() == "v2":
        sources.append(("/api/v2/memory/get", {"memory_type": "profile", "user_id": owner()}))
    if read_v1():
        sources.append(("/api/v1/memories/get", {"filters": {"user_id": owner()}, "memory_type": "profile"}))
    results = _read_jobs({path: (path, body) for path, body in sources}, READ_TIMEOUT * 2 if budget is None else budget)
    for path, _ in sources:
        profiles = (results.get(path) or {}).get("profiles") or []
        ids = with_ids and path.startswith("/api/v2")  # 只有 v2 的条目能编辑
        for line in _profile_lines(profiles[0].get("profile_data") or {}, ids) if profiles else []:
            key = line[:40]
            if key not in seen:
                seen.add(key)
                lines.append(line)
    return "\n".join(lines)[:limit]


def substantial(query: str) -> bool:
    """v1 没有得分没法过滤噪声，只在提问有实际内容时才带上 v1 结果。"""
    return len(re.sub(r"\s|[，。！？、,.!?~]", "", query)) >= 6


def _recall_text(row: dict, first: str, second: str, separator: str | None = None) -> str:
    values = [str(row.get(key) or "").strip() for key in (first, second)]
    if separator is None:
        return values[0] or values[1]
    return separator.join(values) if any(values) else ""


def _has_search_hits(name: str, data: dict) -> bool:
    if name == "v2u":
        if any(_recall_text(ep, "summary", "subject") for ep in data.get("episodes") or []):
            return True
        for profile in data.get("profiles") or []:
            pdata = profile.get("profile_data") or {}
            rows = (pdata.get("explicit_info") or []) + (pdata.get("implicit_traits") or [])
            if str(profile.get("description") or "").strip() or any(
                    str(row.get("description") or "").strip() for row in rows):
                return True
        return False
    return any(_recall_text(row, "task_intent", "key_insight", " → ") for row in data.get("agent_cases") or []) or any(
        _recall_text(row, "name", "description", "：") for row in data.get("agent_skills") or [])


def search_items(query: str, top_k: int = 5, kind: str = "all", include_profile: bool = False,
                 budget: float | None = None, v1_episodes: int | None = None,
                 method: str = "hybrid", notices: list[str] | None = None, strict: bool = False) -> list[dict]:
    """并行查 v2 用户轨道、v2 agent 轨道、v1 旧库（过渡期），按得分门槛过滤后合并去重。"""
    if type(top_k) is not int or (top_k != -1 and not 1 <= top_k <= 100):
        raise ValueError("top_k must be -1 or in 1..100")
    if method not in ("hybrid", "agentic"):
        raise ValueError("method must be hybrid or agentic")
    deep = method == "agentic" and version() == "v2"
    total_budget = max(0.0, (agentic_timeout() if deep else READ_TIMEOUT * 2) if budget is None else budget)
    deadline = time.monotonic() + total_budget
    if total_budget <= 0:
        return []
    notes = notices if notices is not None else []
    if method == "agentic" and not deep:
        notes.append("（当前使用 v1，agentic 不可用，本次按 hybrid 检索。）")
    # Presentation truncation appends a notice. Keep the bridge's query budget
    # on the entire transmitted string, without adding display text to searches.
    q, k = str(query).strip()[:500], top_k
    jobs = {}
    if version() == "v2":
        if kind in ("all", "episodes"):
            body = {"query": q, "user_id": owner(), "method": method, "top_k": k,
                    "include_profile": include_profile}
            if method == "hybrid":
                body["min_score"] = _num("EVEROS_MIN_SCORE", 0.3)
            jobs["v2u"] = ("/api/v2/memory/search", body)
        if kind in ("all", "practices"):
            jobs["v2a"] = ("/api/v2/memory/search", {"query": q, "agent_id": agent_id(), "method": method, "top_k": k})
    if v1_episodes is None:
        v1_episodes = k
    if read_v1():
        types = {"episodes": ["episodic_memory"], "practices": ["agent_memory"]}.get(
            kind, ["episodic_memory", "agent_memory"])
        jobs["v1"] = ("/api/v1/memories/search", {"filters": {"user_id": owner()}, "query": q, "method": "hybrid",
                                                  "top_k": k, "memory_types": types})
    reserve = min(FALLBACK_TIMEOUT, total_budget / 3) if deep else 0.0
    agent_method = method
    if deep:
        results, methods = _agentic_jobs(jobs, deadline, reserve, notes)
        agent_method = methods.get("v2a", method)
    else:
        results = _read_jobs(jobs, max(0.0, deadline - time.monotonic()))
    if (method == "agentic" or strict) and jobs and not results:
        raise APIResponseError("记忆检索失败或超时，未获得任何成功轨道；请重试。")
    if strict and results:
        labels = {"v2u": "用户记忆", "v2a": "做法记忆", "v1": "v1 旧库"}
        missing = [labels[name] for name in jobs if name not in results]
        if missing:
            notes.append(f"（{'、'.join(missing)}检索失败或超时，以下仅展示成功轨道的结果。）")
    items = []
    v2u, v2a, v1 = results.get("v2u") or {}, results.get("v2a") or {}, results.get("v1") or {}

    def add(row, kind, text, source, **extra):
        import recall
        item = dict(id=row.get("id"), kind=kind, text=text, **extra)
        try:
            item = recall.snapshot(item, row, source)
        except OSError as exc:
            log(f"recall snapshot unavailable: {type(exc).__name__}")
        items.append(item)

    for ep in v2u.get("episodes") or []:
        add(ep, "episode", _recall_text(ep, "summary", "subject"), "v2/episode",
            date=str(ep.get("timestamp", ""))[:10], score=float(ep.get("score") or 0))
    for pr in v2u.get("profiles") or []:
        pdata = pr.get("profile_data") if isinstance(pr, dict) and "profile_data" in pr else None
        lines = _profile_lines(pdata)[:6] if pdata else [f"- {pr.get('description', '')}"]
        for line in lines:
            add({"id": "profile:" + line[:40], "description": line[2:]}, "profile", line[2:], "v2/profile")
    for key, kind, fields in (("agent_cases", "case", ("task_intent", "key_insight", " → ")),
                              ("agent_skills", "skill", ("name", "description", "："))):
        for row in v2a.get(key) or []:
            if agent_method == "agentic" or float(row.get("score") or 0) >= AGENT_MIN_SCORE:
                add(row, kind, _recall_text(row, *fields), "v2/" + kind)
    v1_limit = None if v1_episodes == -1 else v1_episodes
    for ep in (v1.get("episodes") or [])[:v1_limit]:
        add(ep, "episode", _recall_text(ep, "summary", "subject"), "v1/episode",
            date=str(ep.get("timestamp", ""))[:10], src="v1")
    agent = v1.get("agent_memory") or {}
    for key, kind, fields in (("cases", "case", ("task_intent", "key_insight", " → ")),
                              ("skills", "skill", ("name", "description", "："))):
        for row in agent.get(key) or []:
            if float(row.get("score") or 0) >= AGENT_MIN_SCORE:
                add(row, kind, _recall_text(row, *fields), "v1/" + kind)
    out, seen = [], set()
    for it in items:
        key = re.sub(r"\s", "", str(it["text"]))[:40]
        if it["text"] and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def format_items_with_ids(items: list[dict], limit: int = 4000) -> tuple[str, list[str]]:
    """同时返回真实展示的条目 ID，正文里的换行不影响展示条目数。"""
    label = {"case": "[做法] ", "skill": "[技能] ", "profile": "[画像] "}
    lines, shown = [], []
    for it in items:
        head = f"[{it['date']}] " if it.get("date") else label.get(it["kind"], "")
        meta = (f" [id={it['ref']} source={it['source']} truncated={str(it['truncated']).lower()}]"
                if it.get("ref") else "")
        line = f"- {head}{clip(it['text'], 260)}{meta}"
        if sum(len(x) + 1 for x in lines) + len(line) > limit:
            break
        lines.append(line)
        if it.get("id") is not None:
            shown.append(str(it["id"]))
    return "\n".join(lines), shown


def format_items(items: list[dict], limit: int = 4000) -> str:
    return format_items_with_ids(items, limit)[0]


def search_text(query: str, top_k: int = 5, kind: str = "all", method: str = "hybrid", strict: bool = False,
                budget: float | None = None, context: str = "", session_id: str = "") -> str:
    import recall
    if context or session_id:
        if session_id and not context:
            with recall.locked(session_id) as state:
                context = recall.context_text(state)
        query = recall.query_text(query, context)
    notices = []
    options = {"method": method, "notices": notices, "strict": strict}
    if budget is not None:
        options["budget"] = budget
    items = search_items(query, top_k, kind, **options)
    prefix = "\n".join(notices)
    text, shown = format_items_with_ids(items, limit=max(0, 4000 - len(prefix) - bool(prefix)))
    if session_id:
        with recall.locked(session_id) as state:
            recall.record_coverage(state, query, items, shown)
    return prefix + "\n" + (text or "（没有相关记忆）") if prefix else text


def recent_text(days: int = 7, limit: int = 20) -> str:
    cutoff = time.time() - days * 86400
    rows = []
    if version() == "v2":
        try:
            out = api("/api/v2/memory/get", {"memory_type": "episode", "user_id": owner(), "page_size": min(limit, 100),
                                             "sort_by": "timestamp", "sort_order": "desc",
                                             "filters": {"timestamp": {"gte": int(cutoff * 1000)}}}, timeout=READ_TIMEOUT * 2)
            rows += (out.get("data") or {}).get("episodes") or []
        except Exception as exc:
            log(f"recent v2 failed: {exc!r}")
    if read_v1():
        page = 1
        while page <= 5:
            out = api("/api/v1/memories/get", {"filters": {"user_id": owner()}, "memory_type": "episodic_memory",
                                               "page": page, "page_size": 50}, timeout=READ_TIMEOUT * 2)
            eps = (out.get("data") or {}).get("episodes") or []
            fresh = [e for e in eps if ts_ms(str(e.get("timestamp"))) / 1000 >= cutoff]
            rows += fresh
            if len(fresh) < len(eps) or not eps:
                break
            page += 1
    rows.sort(key=lambda e: str(e.get("timestamp", "")), reverse=True)
    lines, seen = [], set()
    for ep in rows:
        text = ep.get("subject") or ep.get("summary") or ""
        if text[:40] in seen:
            continue
        seen.add(text[:40])
        lines.append(f"- [{str(ep.get('timestamp', ''))[:10]}] {clip(text, 160)}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


# ---------- 写：结构化记录 ----------

def _v2_messages(msgs: list[dict]) -> list[dict]:
    """v2 按 sender_id 归属：助手与工具消息统一记到 agent_id，原 agent 名放进 sender_name。"""
    out = []
    for m in msgs:
        m = dict(m)
        if m["role"] in ("assistant", "tool"):
            m.setdefault("sender_name", m.get("sender_id") or "agent")
            m["sender_id"] = agent_id()
        out.append(m)
    return out


def post_messages(session_id: str, msgs: list[dict], agent: bool = False, ver: str | None = None) -> str:
    ensure_writable()
    ver = ver or version()
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
        if ver == "v2":
            out = api("/api/v2/memory/add", {"session_id": session_id[:128], "async_mode": False,
                                             "messages": _v2_messages(batch)})
        else:
            out = api("/api/v1/memories/agent" if agent else "/api/v1/memories",
                      {"user_id": owner(), "session_id": session_id[:128], "async_mode": False, "messages": batch})
        status = _write_status(out, {"accumulated", "extracted", "buffered", "no_extraction"}, "add")
    return status


def _write_status(out: dict, accepted: set[str], operation: str) -> str:
    out = _check_response(out)
    data = out.get("data")
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, str) or status not in accepted:
        raise APIResponseError(f"EverOS {operation} 未完成：status={status!r}")
    return status


def flush(session_id: str, agent: bool = False, ver: str | None = None) -> str:
    ensure_writable()
    if (ver or version()) == "v2":
        out = api("/api/v2/memory/flush", {"session_id": session_id[:128]}, timeout=180)
    else:
        out = api("/api/v1/memories/agent/flush" if agent else "/api/v1/memories/flush",
                  {"user_id": owner(), "session_id": session_id[:128]}, timeout=180)
    return _write_status(out, {"extracted", "no_extraction"}, "flush")


def remember(host: str, topic: str, content: str, user_said: str = "") -> str:
    """结构化记录：用户原话（可空）+ 结论/动作/证据，立即抽取。"""
    ensure_writable()
    t = int(time.time() * 1000)
    sid = f"{host}_{datetime.now(CST):%Y%m%d}_{slug(topic) or 'note'}"
    msgs = [{"role": "user", "sender_id": owner(), "timestamp": t - 1,
             "content": clip(user_said or f"（{host} 记录）{topic}")},
            {"role": "assistant", "sender_id": host, "timestamp": t, "content": clip(content)}]
    add = post_messages(sid, msgs)
    time.sleep(1)
    return f"{sid}: {add}/{flush(sid)}"


def _text_field(step: dict, key: str) -> str:
    value = step.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"做法步骤的 {key} 必须是字符串")
    return value.strip()


def validate_practice(steps, conclusion) -> None:
    """所有宿主共用的写前校验：MCP schema 之外，直接调用 core 的入口也要拦住。"""
    if not isinstance(steps, list) or len(steps) < PRACTICE_MIN_STEPS:
        raise ValueError(PRACTICE_STEPS_HINT)
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"做法第 {i} 步必须是对象")
        if not _text_field(step, "action") or not _text_field(step, "result"):
            raise ValueError(f"做法第 {i} 步缺少 action 或 result")
        _text_field(step, "why")
        _text_field(step, "user_correction")
        if (step.get("outcome") or "ok") not in PRACTICE_OUTCOMES:
            raise ValueError(f"做法第 {i} 步的 outcome 只能是 {'、'.join(PRACTICE_OUTCOMES)}")
    if not isinstance(conclusion, str) or not conclusion.strip():
        raise ValueError("做法的 conclusion 不能为空：写根因、解法和验证方式")


def practice_messages(host: str, task: str, steps: list[dict], conclusion: str,
                      now_ms: int | None = None) -> list[dict]:
    """需求 → 每步（动作与原因 → 结果 → 没走通或意外时的复盘 → 用户纠正）→ 结论。

    v2 提炼 case 前由 LLM 判断轨迹里有没有试错、意外或用户纠正；判断时工具结果只看开头约 80 token，
    助手文字约 600 token。所以原因和复盘写进助手文字，用户纠正作为独立的 user 消息。
    """
    owner_id = owner()
    body = []
    for i, step in enumerate(steps, 1):
        action, result = _text_field(step, "action"), _text_field(step, "result")
        why, correction = _text_field(step, "why"), _text_field(step, "user_correction")
        mark = PRACTICE_OUTCOMES[step.get("outcome") or "ok"]
        body.append({"role": "assistant", "sender_id": host,
                     "content": clip(action + (f"\n原因：{why}" if why else "")),
                     "tool_calls": [{"id": f"call_{i}", "type": "function",
                                     "function": {"name": "step", "arguments": json.dumps(
                                         {"action": action}, ensure_ascii=False)}}]})
        body.append({"role": "tool", "sender_id": host, "tool_call_id": f"call_{i}",
                     "content": clip(f"【{mark[0]}】{result}" if mark else result)})
        if mark:
            body.append({"role": "assistant", "sender_id": host, "content": clip(f"{mark[1]}：{clip(result, 400)}")})
        if correction:
            body.append({"role": "user", "sender_id": owner_id, "content": clip(correction)})
    msgs = [{"role": "user", "sender_id": owner_id, "content": clip(task)}, *body,
            {"role": "assistant", "sender_id": host, "content": clip(conclusion)}]
    start = (int(time.time() * 1000) if now_ms is None else now_ms) - len(msgs)
    for offset, msg in enumerate(msgs):
        msg["timestamp"] = start + offset
    return msgs


def find_practice_case(session_id: str, since_ms: int, timeout: float = 10) -> dict | None:
    """在 v2 做法 case 里找本次写入生成的那条：同一 session，且不早于本次轨迹的首条消息。

    服务端按 session_id 过滤；本地仍逐条核对，同一天同一 topic 的旧 case 不算数。
    """
    out = api("/api/v2/memory/get", {"memory_type": "agent_case", "agent_id": agent_id(), "page_size": 20,
                                     "sort_by": "timestamp", "sort_order": "desc",
                                     "filters": {"session_id": session_id[:128]}}, timeout=timeout)
    for case in (out.get("data") or {}).get("agent_cases") or []:
        if case.get("session_id") == session_id[:128] and epoch_ms(case.get("timestamp")) >= since_ms - 1000:
            return case
    return None


def practice_case_note(session_id: str, since_ms: int, started: float) -> str:
    """写后回读：在 EVEROS_CASE_WAIT 秒内轮询 v2 做法 case，结果只作提示，不影响写入成败。"""
    wait = case_wait()
    if wait <= 0:
        return "v2 做法 case 未回读（EVEROS_CASE_WAIT=0）"
    deadline, error = started + wait, None
    while True:
        try:
            case = find_practice_case(session_id, since_ms,
                                      timeout=max(1.0, min(10.0, deadline - time.monotonic())))
            error = None
        except Exception as exc:
            case, error = None, exc
        if case:
            elapsed = time.monotonic() - started
            log(f"practice case {session_id} found {elapsed:.0f}s quality={case.get('quality_score')}")
            return f"v2 做法 case 已生成（质量 {case.get('quality_score')}，约 {elapsed:.0f} 秒）"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(CASE_POLL, remaining))
    if error is not None:
        log(f"practice case {session_id} check failed: {error!r}")
        return f"v2 做法 case 回读失败（{error}）；写入本身已完成"
    log(f"practice case {session_id} missing after {wait:.0f}s")
    return (f"v2 做法 case 在 {wait:.0f} 秒内没有生成。可能没通过 v2 的提炼筛选：步骤都是顺利执行、"
            "没写出失败尝试或意外发现、没有用户纠正，或者轨迹过长；也可能还在后台处理。"
            "轨迹已作为普通记忆写入，检索仍能查到；稍后可用 memory_search(kind=practices) 复查")


def record_practice(host: str, topic: str, task: str, steps: list[dict], conclusion: str) -> str:
    """做法轨迹：需求 → 每步动作与结果 → 结论。过渡期双写 v1 agent 轨道；写完回读 v2 是否生成做法 case。"""
    ensure_writable()
    validate_practice(steps, conclusion)
    sid = f"agent_{datetime.now(CST):%Y%m%d}_{slug(topic) or 'practice'}"
    msgs = practice_messages(host, task, steps, conclusion)
    results, failed, v2_done = [], False, None
    vers = ["v2", "v1"] if version() == "v2" and read_v1() else [version()]
    for ver in vers:
        add = None
        try:
            add = post_messages(sid, msgs, agent=True, ver=ver)
            time.sleep(1)
            results.append(f"{ver} {add}/{flush(sid, agent=True, ver=ver)}")
            if ver == "v2":
                v2_done = time.monotonic()
        except Exception as exc:
            failed = True
            phase = f"（add={add}，flush 未完成）" if add is not None else ""
            results.append(f"{ver} failed{phase}: {exc!r}")
    result = f"{sid}: " + "; ".join(results)
    if failed:
        log(f"practice incomplete {result}")
        raise RuntimeError("做法记录未完整写入；" + result)
    if v2_done is not None:
        result += "；" + practice_case_note(sid, msgs[0]["timestamp"], v2_done)
    return result


def fix_profile(operations: list[dict]) -> str:
    """v2 直接改画像：action=add/update/delete，type=explicit_info/implicit_traits，带 reason。"""
    ensure_writable()
    if version() != "v2":
        raise RuntimeError("编辑画像需要 v2（EVEROS_API_VERSION=v2）")
    selected = operations[:50]
    if not selected:
        raise ValueError("画像编辑至少需要一条操作")
    out = _check_response(api("/api/v2/memory/edit", {"user_id": owner(), "operations": selected}))
    data = out.get("data") or out
    if not isinstance(data, dict):
        raise APIResponseError("画像编辑返回了无效结果对象")
    results = data.get("results")
    if type(data.get("applied")) is not int or data.get("applied") != len(selected) or (isinstance(results, list) and any(
            not isinstance(it, dict) or it.get("error") or it.get("status") != "applied" for it in results)):
        raise APIResponseError(f"画像编辑未完整应用：applied={data.get('applied')!r}/{len(selected)}")
    return json.dumps({"applied": data.get("applied"), "results": data.get("results")}, ensure_ascii=False)[:1500]


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
    if trae.is_trae(entries, transcript):
        return "trae"
    if "/.codex/" in transcript or any(o.get("type") in ("session_meta", "response_item") for o in entries[:50]):
        return "codex"
    # 桌面端（Kimi Work）与 Kimi Code CLI 的 wire.jsonl 同构，只能靠路径区分；必须先于 kimi 判断
    if "kimi-desktop" in transcript:
        return "kimi_desktop"
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


def desktop_home() -> Path:
    """Kimi Work 桌面端内嵌 kimi-code 的 home（daimon 托管，App 升级可能重建）。"""
    if os.environ.get("EVEROS_KIMI_DESKTOP_HOME"):
        return Path(os.environ["EVEROS_KIMI_DESKTOP_HOME"]).expanduser()
    return (Path.home() / "Library/Application Support/kimi-desktop/daimon-share/daimon"
            / "runtime/kimi-code/home")


def desktop_transcript(session_id: str) -> Path | None:
    """桌面端会话目录是 sessions/wd_*/<conv-xxx>/agents/main/wire.jsonl，先查 session_index.jsonl 精确映射。"""
    if not session_id or session_id.startswith("ctitle-"):  # ctitle-* 是标题生成侧会话，不采集
        return None
    home = desktop_home()
    idx = home / "session_index.jsonl"
    if idx.is_file():
        try:  # 索引按时间追加，倒序找最新映射
            for line in reversed(idx.read_text(encoding="utf-8").splitlines()):
                if f'"sessionId":"{session_id}"' not in line:
                    continue
                p = Path(json.loads(line).get("sessionDir") or "") / "agents/main/wire.jsonl"
                if p.is_file():
                    return p
        except Exception:
            pass
    hits = sorted(home.glob(f"sessions/*/{session_id}/agents/main/wire.jsonl"))
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


def messages_trae(entries: list[dict], host: str = "trae") -> list[dict]:
    """TraeX original dialogue, from modern mutations or legacy response items."""
    msgs = []
    for entry in trae.message_entries(entries):
        item = entry["payload"]
        text = "\n".join(part.get("text", "") for part in item.get("content", [])
                         if isinstance(part, dict) and part.get("type") in ("input_text", "output_text")).strip()
        if not text:
            continue
        if item["role"] == "user":
            kinds = (item.get("internal_chat_message_metadata_passthrough") or {}).get("content_item_kinds")
            # Explicit user.text wins over a prefix that merely resembles scaffolding.
            if kinds is None and text.startswith(_CODEX_SCAFFOLD + _SKIP_PREFIX + (
                    "<system-reminder>", "<skills_instructions>", "<plugins_instructions>", "<collaboration_mode>",
                    "<multi_agent_mode>", "# Environment")):
                continue
            text, sender = split_botmux(text)
        else:
            sender = {"sender_id": host}
        msgs.append({"role": item["role"], **sender, "timestamp": ts_ms(entry.get("timestamp")), "content": clip(text)})
    return _monotonic(msgs)


def messages_kimi(entries: list[dict], host: str = "kimi") -> list[dict]:
    """Kimi Code / Kimi Work 桌面端 wire.jsonl：用户原话在 context.append_message（origin=user），回复在 content.part（text）。"""
    msgs: list[dict] = []
    for o in entries:
        t = o.get("type")
        if t == "context.append_message":
            m = o.get("message") or {}
            if m.get("role") != "user" or (m.get("origin") or {}).get("kind") != "user":
                continue
            text = "\n".join(c.get("text", "") for c in m.get("content") or [] if isinstance(c, dict)
                             and c.get("type") == "text")
            text = _DESKTOP_META.sub("", text).strip()  # 桌面端 <meta .../> 前导脚手架
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


def messages_mimo(rows: list[dict], host: str = "mimocode") -> list[dict]:
    """MiMoCode Plugin session.post 的 TrajectoryMessage 切片：用户/助手可见文本，剔除 synthetic 与 reasoning。"""
    msgs: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("role") not in ("user", "assistant"):
            continue
        texts = []
        for part in row.get("parts") or []:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            if part.get("synthetic") or part.get("ignored"):
                continue
            text = str(part.get("text") or "").strip()
            if text:
                texts.append(text)
        content = "\n".join(texts).strip()
        if row.get("role") == "assistant":
            content = str(row.get("finalText") or content).strip()
        if not content:
            continue
        created = int(row.get("created") or 0) or int((row.get("time") or {}).get("created") or 0)
        if 0 < created < 10**12:  # 秒级时间戳升到毫秒
            created *= 1000
        msgs.append({"role": row["role"],
                     "sender_id": owner() if row["role"] == "user" else host,
                     "timestamp": created or ts_ms(None),
                     "content": clip(content)})
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
    prefix = {"claude_code": "cc", "codex": "cx", "trae": "tx", "kimi": "km", "kimi_desktop": "kw",
              "mimocode": "mc"}.get(host, slug(host, 12) or "agent")
    # TraeX uses UUIDv7: the first eight characters are a coarse timestamp,
    # shared by sessions started within about a minute. Keep the complete ID.
    session_suffix = sid if host == "trae" else sid[:8]
    return f"{prefix}_{day:%Y%m%d}_{slug(title or Path(cwd or 'x').name) or 'session'}_{session_suffix}"


def write_turns(payload: dict) -> None:
    ensure_writable()
    sid = str(payload.get("session_id") or "")
    tpath = Path(str(payload.get("transcript_path") or ""))
    event = str(payload.get("hook_event_name") or "")
    host_hint = os.environ.get("EVEROS_HOST", "")
    if not tpath.is_file() and sid:
        if host_hint == "trae":
            tpath = trae.transcript(sid) or tpath
            if not tpath.is_file():
                raise FileNotFoundError(f"TraeX rollout not found for session {sid}")
        elif host_hint == "kimi_desktop" or sid.startswith(("conv-", "ctitle-")):
            tpath = desktop_transcript(sid) or tpath
        elif host_hint == "kimi" or str(payload.get("client_type", "")).startswith("kimi"):
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
            host = "trae" if host_hint == "trae" else detect_host(head, str(tpath))
            st.update(claude_session_id=sid, host=host, cwd=str(payload.get("cwd") or ""), transcript_path=str(tpath),
                      everos_session_id=session_name(head, host, str(payload.get("cwd") or ""),
                                                     sid.removeprefix("session_"), str(payload.get("session_title") or
                                                     payload.get("thread_name") or payload.get("session_name") or "")))
        host = st.get("host") or "claude_code"
        if st.get("api", "v1") != version() and st.get("pending"):  # 换库前把旧库里的缓冲落盘
            try:
                res = flush(st['everos_session_id'], ver=st.get('api', 'v1'))
                st.update(pending=False, last_flush_ms=int(time.time() * 1000), last_flush_status=res)
                save_state(sp, st)
                log(f"switch flush {st['everos_session_id']} ({st.get('api', 'v1')}) -> {res}")
            except Exception as exc:
                log(f"switch flush failed {st['everos_session_id']}: {exc!r}")
                raise  # 旧库仍 pending，不能换 api 后把旧缓冲的重试路径丢掉
        parse = {"codex": messages_codex, "trae": messages_trae,
                 "kimi": messages_kimi, "kimi_desktop": messages_kimi}.get(host, messages_claude)
        msgs = parse(entries, host)
        if msgs:
            res = post_messages(st["everos_session_id"], msgs)
            st.update(pending=True, api=version(), last_write_ms=int(time.time() * 1000), last_write_status=res,
                      writes=st.get("writes", 0) + 1, messages=st.get("messages", 0) + len(msgs))
            log(f"{event} add {st['everos_session_id']} +{len(msgs)} -> {res}")
        st["offset"] = new_offset
        save_state(sp, st)
        if event in ("SessionEnd", "PreCompact") and st.get("pending"):
            try:
                res = flush(st["everos_session_id"], ver=st.get("api"))
                st.update(pending=False, last_flush_ms=int(time.time() * 1000), last_flush_status=res)
                save_state(sp, st)
                log(f"{event} flush {st['everos_session_id']} -> {res}")
            except Exception as exc:  # 留给 sweep 补
                log(f"{event} flush failed {st['everos_session_id']}: {exc!r}")
    sweep()


def write_mimo_turns(payload: dict) -> None:
    """MiMoCode Plugin session.post：轨迹已在内存，无需 transcript；每轮写完即 flush。"""
    ensure_writable()
    sid = str(payload.get("session_id") or "")
    if not sid:
        return
    host = "mimocode"
    cwd = str(payload.get("cwd") or "")
    title = str(payload.get("title") or "")
    msgs = messages_mimo(payload.get("trajectory") or [], host=host)
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    sp = SESS_DIR / f"{sid}.json"
    with open(sp.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = load_state(sp)
        if not st.get("everos_session_id"):
            st.update(claude_session_id=sid, host=host, cwd=cwd,
                      everos_session_id=session_name([], host, cwd, sid.removeprefix("session_"), title))
        if msgs:
            res = post_messages(st["everos_session_id"], msgs)
            st.update(pending=True, api=version(), last_write_ms=int(time.time() * 1000), last_write_status=res,
                      writes=st.get("writes", 0) + 1, messages=st.get("messages", 0) + len(msgs))
            log(f"mimocode add {st['everos_session_id']} +{len(msgs)} -> {res}")
        save_state(sp, st)
        if st.get("pending"):
            try:
                res = flush(st["everos_session_id"], ver=st.get("api"))
                st.update(pending=False, last_flush_ms=int(time.time() * 1000), last_flush_status=res)
                save_state(sp, st)
                log(f"mimocode flush {st['everos_session_id']} -> {res}")
            except Exception as exc:
                log(f"mimocode flush failed {st['everos_session_id']}: {exc!r}")
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
    ensure_writable()
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
            attempts = prev.get("attempts", 0) if prev.get("attempted_mtime") == mtime else 0
            if prev.get("mtime", 0) >= mtime or attempts >= 3:
                continue
            if limit is not None and done >= limit:
                break
            meta, body = _frontmatter(f.read_text(encoding="utf-8"))
            project = re.sub(r"^-Users-[^-]+-(GolandProjects-)?", "", f.parent.parent.name).replace("-", "_")
            sid = f"cc_localmem_{project}_{meta.get('name') or f.stem}"[:128]
            # 时间戳用同步时刻：v1 会静默丢弃约 2 个月前的时间戳（v2 实测 62 天前仍可入库）
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
                # flush 已验证 extracted/no_extraction：后者是空缓冲或内容去重，同样完成。
                ledger[key] = {"mtime": mtime, "attempted_mtime": mtime, "session": sid, "add": add, "flush": res,
                               "attempts": 0,
                               "synced_at": datetime.now(CST).isoformat(timespec="seconds")}
                save_state(LOCALMEM_LEDGER, ledger)
                log(f"localmem {sid} -> {add}/{res}")
                done += 1
            except Exception as exc:  # 单条超时跳过；连续 3 次失败说明网络或鉴权有问题，停手
                # 业务未完成只限制本文件版本；网络故障不消耗内容重试次数。
                ledger[key] = {**prev, "attempted_mtime": mtime, "session": sid,
                               "attempts": attempts + 1 if isinstance(exc, APIResponseError) else attempts,
                               "last_error": str(exc)[:200]}
                save_state(LOCALMEM_LEDGER, ledger)
                log(f"localmem failed {sid}: {exc!r}")
                done += 1
                fails += 1
                if fails >= 3:
                    break
                continue
            fails = 0


def sweep(force: bool = False) -> None:
    """补 flush 闲置会话并同步本地记忆；限频，避免每轮都扫。"""
    ensure_writable()
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
                    res = flush(st["everos_session_id"], ver=st.get("api", "v1"))
                    st.update(pending=False, last_flush_ms=int(now * 1000), last_flush_status=res)
                    save_state(p, st)
                    log(f"sweep flush {st['everos_session_id']} -> {res}")
                except Exception as exc:
                    log(f"sweep flush failed {st.get('everos_session_id')}: {exc!r}")
    sync_local_memory(limit=None if force else 10)


def doctor_result() -> tuple[str, bool]:
    """返回可读诊断及健康状态，供安装器用退出码判断真实结果。"""
    c = cfg()
    healthy = ready()
    lines = [f"EVEROS_HOME   {HOME}（config.env {'存在' if (HOME / 'config.env').is_file() else '无'}）",
             f"API           {version()}{'，过渡期同时读 v1' if version() == 'v2' and read_v1() else ''}；base {c['EVEROS_API_BASE']}",
             f"user_id       {c.get('EVEROS_USER_ID') or '缺失：设置 EVEROS_USER_ID'}",
             "API key       " + ("已配置" if c.get("EVEROS_API_KEY") else
                                  "未配置，云端走网络密钥" if network_secret_mode() else
                                  "缺失：设置 EVEROS_API_KEY 或写入 config.env"),
             f"读侧注入      {'关' if c.get('EVEROS_INJECT') == '0' else '开'}；写侧 {'关' if c.get('EVEROS_WRITE') == '0' else '开'}；"
             f"min_score {_num('EVEROS_MIN_SCORE', 0.3)}；每轮预算 {_num('EVEROS_RECALL_TIMEOUT', 18):.0f}s / {int(_num('EVEROS_RECALL_CHARS', 2000))} 字"]
    checks = []
    if version() == "v2" and c.get("EVEROS_USER_ID"):
        checks += [("v2 用户记忆", "/api/v2/memory/get", {"memory_type": "episode", "user_id": owner(), "page_size": 1}),
                   ("v2 做法 case", "/api/v2/memory/get", {"memory_type": "agent_case", "agent_id": agent_id(), "page_size": 1})]
    if read_v1() and c.get("EVEROS_USER_ID"):
        checks.append(("v1 旧库", "/api/v1/memories/get", {"filters": {"user_id": owner()}, "memory_type": "episodic_memory",
                                                            "page": 1, "page_size": 1}))
    for name, path, body in checks:
        try:
            t = time.time()
            out = _check_response(api(path, body, timeout=15))
            if not isinstance(out.get("data"), dict):
                raise APIResponseError("诊断读取缺少 data 结果对象")
            lines.append(f"{name:<10}  OK，共 {out['data'].get('total_count')} 条，耗时 {time.time() - t:.1f}s")
        except Exception as exc:
            healthy = False
            lines.append(f"{name:<10}  失败：{exc!r}（云端环境需放行 api.evermind.ai）")
    lines.append(f"状态目录      {STATE}（{len(list(SESS_DIR.glob('*.json')))} 个会话账本）")
    return "\n".join(lines), healthy


def doctor() -> str:
    return doctor_result()[0]
