"""Doubao Work (豆包办公) host integration: trajectory parsing and incremental capture.

Doubao Work has no MCP registration or lifecycle hooks for agents. Capture is therefore
driven manually (or from a user-level instruction) through ``doubao.py`` and writes through
the shared EverOS core (offset ledger, add/flush, sweep) like every other host.

Trajectory layout (internal harness format; may change across harness versions):

  ~/.doubao/agent_mode/workspace/.sessions/<sid>/agents/<agent>/
      system/assignment.md       per-turn user request timestamps (ISO, headings)
      system/trajectory.jsonl    OpenAI-style chat messages, one JSON object per line

Only the main agent's transcript is read (assignment.md marks ``Role: main``). Rows have no
timestamps, so user-message times come from assignment.md headings in order; assistant final
answers are timestamped just before the following user message. Tool calls, tool results and
in-progress narration are never uploaded: only role=user text (system-reminder stripped) and
role=assistant rows without tool_calls (final answers).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import core

HOST = "doubao_work"
SESSIONS_ROOT = Path(os.environ.get(
    "DOUBAO_SESSIONS_ROOT", str(Path.home() / ".doubao" / "agent_mode" / "workspace" / ".sessions")))
SID_RE = re.compile(r"^\d{6,32}$")
ASSIGN_TS = re.compile(r"^## \[(\d{4}-\d{2}-\d{2}T[0-9:.]+Z)\] 需求\s*$", re.M)


def parse_ts(value: str) -> int:
    """Assignment heading timestamp -> epoch milliseconds."""
    value = value.rstrip("Z")
    if "." in value:
        head, frac = value.split(".", 1)
        value = f"{head}.{frac[:6]}"
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def clean_user(text: str) -> str:
    """Strip injected memory, external app dumps, and system-reminder blocks."""
    cleaned = core._EXTERNAL_CODEX_PAGE.sub("", core.strip_injected_memory(text))
    return core._REMINDER.sub("", cleaned).strip()


def text_of(content) -> str:
    """Accept both plain-string content and OpenAI structured-content lists."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text") and part.get("text"):
                parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _is_final(row: dict) -> bool:
    return (row.get("role") == "assistant" and not row.get("tool_calls")
            and row.get("channel") in (None, "final") and bool(text_of(row.get("content")).strip()))


def _indexed_messages(rows: list[dict], user_ts: list[int | None], fallback_ms: int):
    out, pending = [], []
    u_idx, last_user, skip_heartbeat = -1, None, False
    for i, row in enumerate(rows):
        role = row.get("role")
        if role == "user":
            u_idx += 1
            text = clean_user(text_of(row.get("content")))
            if not text:
                continue
            ts = user_ts[u_idx] if u_idx < len(user_ts) else None
            for ai, answer in pending:
                out.append((ai, {"role": "assistant", "sender_id": HOST,
                                 "timestamp": ts - 1 if ts else fallback_ms, "content": core.clip(answer)}))
            pending = []
            if core.is_heartbeat_text(text):
                skip_heartbeat = True
                continue
            skip_heartbeat = False
            if text == last_user:
                continue
            last_user = text
            out.append((i, {"role": "user", "sender_id": core.owner(),
                            "timestamp": ts or fallback_ms, "content": core.clip(text)}))
        elif _is_final(row):
            if skip_heartbeat:
                continue
            last_user = None
            pending.append((i, text_of(row.get("content")).strip()))
    for ai, answer in pending:
        out.append((ai, {"role": "assistant", "sender_id": HOST,
                         "timestamp": fallback_ms, "content": core.clip(answer)}))
    return sorted(out, key=lambda item: item[0])


def messages_doubao(rows: list[dict], user_ts: list[int], fallback_ms: int) -> list[dict]:
    return core._monotonic([m for _, m in _indexed_messages(rows, user_ts, fallback_ms)])


def assignment_timestamps(assign_text: str, rows: list[dict] | None = None) -> list[int | None]:
    headings = list(ASSIGN_TS.finditer(assign_text))
    entries = []
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(assign_text)
        text = clean_user(assign_text[heading.end():end])
        if text:
            entries.append((text, parse_ts(heading.group(1))))
    if rows is None:
        return [ts for _, ts in entries]
    result, cursor, previous, previous_ts = [], 0, None, None
    for row in rows:
        if _is_final(row):
            previous, previous_ts = None, None
        if row.get("role") != "user":
            continue
        text = clean_user(text_of(row.get("content")))
        if not text:
            result.append(None)
            continue
        if text == previous:
            result.append(previous_ts)
            continue
        match = next((i for i in range(cursor, len(entries)) if entries[i][0] == text), None)
        if match is not None:
            ts, cursor = entries[match][1], match + 1
        else:
            # Unknown historical times stay unknown; never borrow a different request's date.
            ts = None
        result.append(ts)
        previous, previous_ts = text, ts
    return result



def main_agent_dir(sessions_root: Path, sid: str) -> Path:
    """Locate the main agent directory for a Doubao Work session (assignment.md Role: main)."""
    base = sessions_root / sid / "agents"
    if not base.is_dir():
        raise FileNotFoundError(f"会话目录不存在: {base}")
    explicit, fallback = [], []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.is_symlink():
            continue
        assign = d / "system" / "assignment.md"
        text = assign.read_text(encoding="utf-8") if assign.is_file() else ""
        roles = re.findall(r"^- Role:\s*(\S+)\s*$", text, re.M)
        if "main" in roles:
            explicit.append(d)
        elif not roles and d.name.startswith("m_") and (d / "system/trajectory.jsonl").is_file():
            fallback.append(d)
    candidates = explicit or fallback
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError(f"main agent 不唯一，保留现场：{base}")
    raise FileNotFoundError(f"未找到 main agent: {base}")


def current_session_id() -> str:
    """Infer the current session id from the harness workspace config work_dir."""
    cfg = Path("/run/mcp_vm_server/workspace_config.json")
    work_dir = None
    if cfg.is_file():
        try:
            work_dir = json.loads(cfg.read_text(encoding="utf-8")).get("work_dir")
        except (OSError, json.JSONDecodeError):
            work_dir = None
    work_dir = work_dir or os.getcwd()
    sid = Path(work_dir).name
    if not SID_RE.match(sid):
        raise ValueError(f"无法从工作目录推断会话 ID: {work_dir}")
    return sid


def session_name(sid: str, rows: list[dict], user_ts: list[int]) -> str:
    first = next((clean_user(text_of(r.get("content"))) for r in rows if r.get("role") == "user"), "")
    ts = user_ts[0] / 1000 if user_ts and user_ts[0] is not None else time.time()
    day = datetime.fromtimestamp(ts, core.CST)
    return f"dw_{day:%Y%m%d}_{core.slug(first) or 'session'}_{sid}"[:128]


def read_session(sessions_root: Path, sid: str) -> dict:
    """Read assignment + complete trajectory lines for one session.

    Returns parsed rows, their byte offsets (start of each line) and the file length up to
    the final newline. A half-written trailing line is left for the next run.
    """
    adir = main_agent_dir(sessions_root, sid)
    traj = adir / "system" / "trajectory.jsonl"
    assignment = adir / "system" / "assignment.md"
    assign = assignment.read_text(encoding="utf-8") if assignment.is_file() else ""
    raw = traj.read_bytes()
    end = raw.rfind(b"\n") + 1
    rows, offsets = [], []
    pos = 0
    for line in raw[:end].splitlines(keepends=True):
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"trajectory 完整行 JSON 损坏，byte={pos}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"trajectory 行不是消息对象，byte={pos}")
            rows.append(row)
            offsets.append(pos)
        pos += len(line)
    return {"agent_dir": adir, "trajectory": traj, "rows": rows, "offsets": offsets, "length": end,
            "user_ts": assignment_timestamps(assign, rows),
            # First-row fingerprint: appending later turns must not invalidate it.
            "head_hash": hashlib.sha256(
                json.dumps(rows[0], ensure_ascii=False, sort_keys=True).encode()).hexdigest() if rows else ""}


def _ledger_offset(st: dict, data: dict, sid: str, since_bytes: int | None,
                   legacy_offset_unit: str | None) -> tuple[int, bool, str | None]:
    unit = st.get("offset_unit")
    if unit not in (None, "bytes-v1"):
        raise ValueError("未知 offset_unit；拒绝重放")
    migrate = bool(st) and unit is None and since_bytes is None
    if legacy_offset_unit is not None:
        if legacy_offset_unit not in ("bytes-v1", "rows-v1") or not migrate:
            raise ValueError("legacy-offset-unit 仅用于无单位旧账本，不能覆盖已标记单位或 since_bytes")
    offset = int(st.get("offset", 0)) if since_bytes is None else since_bytes
    source_unit = None
    if migrate:
        saved_unit = st.get("legacy_offset_unit")
        if saved_unit not in (None, "bytes-v1", "rows-v1"):
            raise ValueError("未知旧账本迁移格式；拒绝重放")
        if saved_unit and legacy_offset_unit and saved_unit != legacy_offset_unit:
            raise ValueError("legacy-offset-unit 与已确认旧账本来源冲突")
        source_unit = legacy_offset_unit or saved_unit
        # Only the bridge byte writer saved this identity-bound source tuple.
        if source_unit is None and (isinstance(st.get("identity"), str) and st["identity"]
                and st.get("host") == HOST and st.get("claude_session_id") == sid
                and st.get("transcript_path") == str(data["trajectory"])):
            source_unit = "bytes-v1"
        if source_unit is None:
            raise ValueError("旧账本 offset 单位不明；核对来源后指定 --legacy-offset-unit bytes-v1 或 rows-v1")
        if source_unit == "rows-v1":
            if offset < 0 or offset > len(data["rows"]):
                raise ValueError("旧行偏移越界；拒绝重放")
            offset = data["offsets"][offset] if offset < len(data["rows"]) else data["length"]
    if offset < 0 or offset > data["length"]:
        raise ValueError("字节偏移越界；拒绝重放")
    return offset, migrate, source_unit


def ingest_session(sid: str, *, sessions_root: Path | None = None, dry_run: bool = False,
                   flush: bool = False, since_bytes: int | None = None,
                   legacy_offset_unit: str | None = None) -> dict:
    """Incrementally capture one Doubao Work session into EverOS.

    Offset semantics match core.write_turns: the ledger advances only to a fully-read point
    and only after an accepted add; a changed first line stops instead of silently replaying.
    """
    sessions_root = sessions_root or SESSIONS_ROOT
    if not SID_RE.match(sid):
        raise ValueError(f"非法会话 ID: {sid!r}")
    data = read_session(sessions_root, sid)
    rows, offsets, length = data["rows"], data["offsets"], data["length"]
    fallback = int(data["trajectory"].stat().st_mtime * 1000)
    if not dry_run:
        core.ensure_writable()
        core.SESS_DIR.mkdir(parents=True, exist_ok=True)
    sp = core.SESS_DIR / f"doubao_{sid}.json"
    lock_context = nullcontext(None) if dry_run else open(sp.with_suffix(".lock"), "w")
    with lock_context as lock:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        st = core.load_state(sp)
        offset, migrate, source_unit = _ledger_offset(st, data, sid, since_bytes, legacy_offset_unit)
        head_hash = st.get("head_hash")
        if head_hash and head_hash != data["head_hash"]:
            raise RuntimeError("trajectory 已被改写或截断；为避免重复入库已停止，请人工核对")
        policy = core.write_policy()
        indexed = _indexed_messages(rows, data["user_ts"], fallback)
        msgs = core._monotonic([m for i, m in indexed if offsets[i] >= offset])
        if policy["compact"] and msgs:
            msgs = core.compact_eco_messages(msgs)
        result = {"session": sid, "everos_session_id":
                  st.get("everos_session_id") or session_name(sid, rows, data["user_ts"]),
                  "new_messages": len(msgs), "roles": [m["role"] for m in msgs], "dry_run": dry_run,
                  "offset_before": offset}
        if dry_run:
            result["preview"] = [{"role": m["role"], "timestamp": m["timestamp"], "chars": len(m["content"]),
                                  "head": m["content"][:80]} for m in msgs]
            return result
        core.ensure_writable()
        session_has_user = bool(st.get("has_user") or st.get("messages", 0) > 0
                                or any(m.get("role") == "user" for _, m in indexed))
        if not session_has_user and not st.get("pending") and not st.get("deferred_flush"):
            result["offset_after"] = offset
            result["skipped"] = "no_user_message"
            return result
        if not head_hash:
            st.update(claude_session_id=sid, host=HOST, cwd=str(Path.home() / "Doubao" / "chats" / sid),
                      transcript_path=str(data["trajectory"]), everos_session_id=result["everos_session_id"],
                      head_hash=data["head_hash"])
        if migrate:
            backup = sp.with_name(sp.name + ".bak-offset-" + uuid.uuid4().hex)
            with open(backup, "xb") as out:
                os.chmod(backup, 0o600)
                out.write(sp.read_bytes())
            st["legacy_offset_unit"] = source_unit
        new_offset = length
        core.prepare_session_version(st, sp)
        if session_has_user:
            st["has_user"] = True
        if msgs:
            if policy["auto_capture"] and core.allow_auto_write(policy):
                batches = len(core._split_batches(msgs))
                res = core.post_messages(st["everos_session_id"], msgs)
                if res not in {"accumulated", "extracted", "buffered", "no_extraction"}:
                    raise core.APIResponseError("Doubao add 未确认成功")
                core.record_auto_write(batches, msgs)
                st.update(pending=True, api=core.version(), last_write_ms=int(time.time() * 1000),
                          last_write_status=res, writes=st.get("writes", 0) + 1,
                          messages=st.get("messages", 0) + len(msgs),
                          last_sent_role=msgs[-1]["role"])
                core.log(f"doubao_work add {st['everos_session_id']} +{len(msgs)} -> {res}")
                result["add"] = res
                new_offset = length  # messages may include finals read before this byte boundary; advance to read end
            else:
                result["skipped"] = "curated"
        st.update(offset=new_offset, offset_unit="bytes-v1")
        st.pop("legacy_offset_unit", None)
        core.save_state(sp, st)
        if flush and st.get("pending"):
            res = core.flush(st["everos_session_id"], ver=st.get("api"))
            if res not in {"extracted", "no_extraction"}:
                raise core.APIResponseError("Doubao flush 未确认成功")
            st.update(pending=False, last_flush_ms=int(time.time() * 1000), last_flush_status=res)
            core.save_state(sp, st)
            core.log(f"doubao_work flush {st['everos_session_id']} -> {res}")
            result["flush"] = res
        result["offset_after"] = st["offset"]
    return result


def _aligned_ts(user_ts: list[int], new_rows: list[tuple[int, dict]], all_rows: list[dict]) -> list[int]:
    """Slice the assignment timestamp list to align with user rows inside ``new_rows``.

    Timestamps align with the global role=user sequence, so count user rows preceding the
    first new row and slice accordingly.
    """
    start_idx = new_rows[0][0] if new_rows else 0
    n_before = sum(1 for r in all_rows[:start_idx] if r.get("role") == "user")
    return user_ts[n_before:]


def list_recent_sessions(sessions_root: Path | None = None, since: float = 0.0) -> list[str]:
    sessions_root = sessions_root or SESSIONS_ROOT
    out = []
    if sessions_root.is_dir():
        for d in sorted(sessions_root.iterdir()):
            if not SID_RE.match(d.name) or not d.is_dir():
                continue
            trajs = list((d / "agents").glob("*/system/trajectory.jsonl"))
            if trajs and max(t.stat().st_mtime for t in trajs) >= since:
                out.append(d.name)
    return out


# --------------------------------------------------------------------------- CLI

def _ingest_all(args) -> dict:
    since_file = core.STATE / "doubao_ingest_since"
    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00")).timestamp()
    elif since_file.is_file():
        since = float(since_file.read_text().strip())
    else:
        raise ValueError(f"缺少接入时间点 {since_file}；用 --since <ISO时间> 显式指定")
    results, failed = [], 0
    for sid in list_recent_sessions(Path(args.sessions_root), since):
        try:
            r = ingest_session(sid, sessions_root=Path(args.sessions_root), dry_run=args.dry_run,
                               flush=args.flush)
            item = {"session": sid, "new_messages": r["new_messages"]}
            for key in ("add", "flush"):
                if key in r:
                    item[key] = r[key]
            results.append(item)
        except Exception as exc:
            failed += 1
            core.log_error(f"doubao_work ingest failed {sid}", exc)
            results.append({"session": sid, "error": f"{type(exc).__name__}: {exc}"})
    print(json.dumps({"since": datetime.fromtimestamp(since, core.CST).isoformat(timespec="seconds"),
                      "dry_run": args.dry_run, "sessions": results}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Doubao Work 会话原文入库 EverOS（只写用户原话与最终回复）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--current", action="store_true", help="当前会话（从工作目录推断）")
    group.add_argument("--session", help="指定会话 ID")
    group.add_argument("--all", action="store_true", help="补采接入时间点之后有更新的会话")
    parser.add_argument("--sessions-root", default=str(SESSIONS_ROOT), help="sessions 根目录（默认按环境探测）")
    parser.add_argument("--since", help="配合 --all：ISO 时间，覆盖默认的接入时间点")
    parser.add_argument("--dry-run", action="store_true", help="只解析预览，不调用 API")
    parser.add_argument("--flush", action="store_true", help="写入后立即落盘抽取")
    parser.add_argument("--legacy-offset-unit", choices=["bytes-v1", "rows-v1"],
                        help="经来源核对后指定单个旧无单位账本的格式；先用 --dry-run 核对")
    args = parser.parse_args(argv)
    if args.all and args.legacy_offset_unit:
        parser.error("--legacy-offset-unit 只能用于 --current 或 --session，不能批量指定")
    if core.write_mode() == "off" and not args.dry_run:
        reason = ("EVEROS_WRITE=0" if str(core.cfg().get("EVEROS_WRITE", "")).strip() == "0"
                  else "EVEROS_WRITE_MODE=off")
        print(json.dumps({"error": f"EverOS 写入已关闭（{reason}）"}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        if args.all:
            return _ingest_all(args)
        sid = current_session_id() if args.current else args.session
        print(json.dumps(ingest_session(sid, sessions_root=Path(args.sessions_root), dry_run=args.dry_run,
                                        flush=args.flush, legacy_offset_unit=args.legacy_offset_unit), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        core.log_error(f"doubao_work ingest failed", exc)
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
