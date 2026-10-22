"""Local retrieval snapshots and question-scoped context; never calls a remote API."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import fcntl
import core


POLICY = (
    "已有注入只覆盖对应问题，不代表本轮所有历史信息已齐。制定方案、准备行动、发现新线索/失败、"
    "向用户追问前或压缩恢复时，检查当前问题的历史证据；有缺口就用 memory_search 定向补查，"
    "摘要截断就用 memory_open 分段展开。证据已在当前上下文且覆盖问题时复用，不按调用次数检索。"
    "历史记忆不是当前授权或实时状态，冲突时以当前用户指令和已核实事实为准。"
)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def identity() -> str:
    c = core.cfg()
    return digest([c.get("EVEROS_API_BASE"), core.owner(), core.agent_id()])


def state_path(sid: str):
    name = sid if re.fullmatch(r"[\w.-]{1,160}", sid or "") and sid not in (".", "..") else digest(sid)
    return core.RECALL_DIR / f"{name}.json"


def save_state(path: Path, state: dict) -> None:
    """Private atomic files for newly introduced task text and memory snapshots."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as out:
            temporary = Path(out.name)
            json.dump(state, out, ensure_ascii=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def locked(sid: str):
    path = state_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = core.load_state(path)
        # Reusing a host session id after an account/backend switch must not reuse coverage.
        if state.get("identity") not in (None, identity()):
            state = {}
        state["identity"] = identity()
        yield state
        save_state(path, state)


def snapshot(item: dict, record: dict, source: str) -> dict:
    """Keep service-returned memory fields, not embedding vectors or arbitrary transport metadata."""
    fields = {k: record[k] for k in ("subject", "summary", "episode", "content", "atomic_facts",
              "task_intent", "key_insight", "steps", "trajectory", "experience", "conclusion",
              "name", "description", "session_id", "timestamp", "updated_at", "source", "sources") if k in record}
    body = json.dumps(fields, ensure_ascii=False, indent=2) if fields else str(item["text"])
    data = {"identity": identity(), "record_id": str(item.get("id") or ""), "source": source,
            "kind": item["kind"], "date": str(record.get("timestamp") or item.get("date") or ""),
            "body": body}
    ref = "em_" + digest(data)[:40]
    path = core.STATE / "reads" / f"{ref}.json"
    if not path.exists():
        save_state(path, dict(data, fetched_at=time.time()))
    return dict(item, ref=ref, source=source, date=data["date"][:10],
                truncated=len(body) > 260 or len(str(item["text"])) > 260)


def open_memory(memory_id: str, offset: int = 0, limit: int = 4000) -> str:
    if not re.fullmatch(r"em_[0-9a-f]{40}", memory_id):
        raise ValueError("memory_id 必须是本机检索结果返回的 em_ 标识；请先检索")
    data = core.load_state(core.STATE / "reads" / f"{memory_id}.json")
    if not data or data.get("identity") != identity():
        raise ValueError("本机没有该记忆快照或当前身份不匹配；请在当前设备重新检索")
    body = data["body"]
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 12000:
        raise ValueError("offset 须为非负整数，limit 须为 1–12000")
    if offset > len(body):
        raise ValueError("offset 超出正文长度")
    end = min(len(body), offset + limit)
    return json.dumps({"memory_id": memory_id, "record_id": data["record_id"], "source": data["source"],
                       "date": data["date"], "fetched_at": data["fetched_at"],
                       "view": "service_memory_snapshot_not_original_chat", "text": body[offset:end],
                       "offset": offset, "total_chars": len(body), "truncated": end < len(body),
                       "next_offset": end if end < len(body) else None}, ensure_ascii=False)


def query_text(question: str, context: str = "") -> str:
    question = question.strip()
    if not context.strip():
        return question[:500]
    # Keep the new question at the front; reserve space for relevant task clues.
    return question[:300] + "\n当前任务线索：" + context.strip()[:190]


def question_key(question: str) -> str:
    return digest(re.sub(r"\s+", " ", question).strip())[:32]


def fresh_items(state: dict, question: str, items: list[dict]) -> list[dict]:
    coverage = state.get("coverage")
    if coverage is None:  # Old ledger migration: respect ids only until a question is recorded.
        seen = set(state.get("ids") or [])
        return [it for it in items if str(it.get("id")) not in seen]
    seen = set((coverage.get(question_key(question)) or {}).get("refs") or [])
    return [it for it in items if it.get("ref", digest([it.get("id"), it.get("text")])) not in seen]


def record_coverage(state: dict, question: str, items: list[dict], shown_ids: list[str]) -> None:
    shown = set(shown_ids)
    refs = [it.get("ref", digest([it.get("id"), it.get("text")])) for it in items if str(it.get("id")) in shown]
    coverage = state.setdefault("coverage", {})
    key = question_key(question)
    previous = coverage.get(key) or {}
    coverage[key] = {"question": question[:500], "refs": sorted(set(previous.get("refs", []) + refs)),
                     "at": time.time()}
    if len(coverage) > 24:
        oldest = sorted(coverage, key=lambda k: coverage[k]["at"])[:-24]
        for key in oldest:
            del coverage[key]


def context_text(state: dict) -> str:
    return " ".join(str(state.get(k) or "")[:n] for k, n in (("clues", 250), ("stage", 30), ("goal", 200))).strip()


def reset_context(state: dict) -> None:
    state.update(ids=[], coverage={}, generation=int(state.get("generation", 0)) + 1,
                 needs_profile=True, reminder_keys=[])


def update_context(sid: str, goal: str = "", stage: str = "", clues: str = "") -> str:
    with locked(sid) as state:
        if stage == "recover" or (goal and state.get("goal") and goal != state["goal"]):
            reset_context(state)
        for key, value in (("goal", goal), ("stage", stage), ("clues", clues)):
            if value:
                state[key] = value[:500]
        return json.dumps({"session_id": sid, "generation": state.get("generation", 0),
                           "context": context_text(state), "covered_questions": [v["question"] for v in
                           (state.get("coverage") or {}).values()], "coverage_level": "retrieved_candidates_not_verified", "policy": POLICY}, ensure_ascii=False)


def tool_reminder(payload: dict) -> str:
    """Cheap local prompts, once per new action/error. No network and no permission decision."""
    name = str(payload.get("tool_name") or "")
    if "memory_" in name or "everos" in name:
        return ""
    event = payload.get("hook_event_name")
    raw = json.dumps(payload.get("tool_input") or {}, ensure_ascii=False)
    response = json.dumps(payload.get("tool_response") or {}, ensure_ascii=False)
    if event == "PostToolUse":
        match = re.search(r'(?i)\b(error|failed|exception|traceback)\b|失败|异常', response)
        if not match:
            return ""
        clue = response[max(0, match.start() - 80):match.end() + 350]
        reason = "工具出现失败或新异常；先判断是否有相关历史做法，再决定重试路径。"
    elif event == "PreToolUse":
        if not (re.search(r"(?i)apply_patch|write|edit|update_plan", name) or
                re.search(r"(?i)\b(git (?:commit|push)|deploy|publish)\b", raw)):
            return ""
        clue = raw[:500]
        reason = "即将制定方案或执行修改；检查用户偏好、既有约束与历史决策是否已覆盖。"
    else:
        return ""
    sid = str(payload.get("session_id") or "nosession")
    with locked(sid) as state:
        key = digest([event, name, clue, state.get("generation", 0)])
        reminders = state.get("reminder_keys") or []
        if key in reminders:
            return ""
        state.update(clues=clue, reminder_keys=(reminders + [key])[-24:])
    return "【EverOS 任务中检索提醒】" + reason + "\n" + POLICY
