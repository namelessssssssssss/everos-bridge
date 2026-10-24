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
    """Strip system-reminder blocks; the harness repeats them on every turn."""
    return core._REMINDER.sub("", text).strip()


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


def messages_doubao(rows: list[dict], user_ts: list[int], fallback_ms: int) -> list[dict]:
    """Turn main-agent trajectory rows into EverOS messages (user + final assistant only).

    ``user_ts`` is the ordered list of per-turn timestamps read from assignment.md; it aligns
    with role=user rows in row order. Assistant finals produced before turn N+1 arrive are
    timestamped one millisecond before that user turn; a trailing final answer (no following
    turn yet) uses ``fallback_ms`` and must therefore be re-evaluated next run.
    Consecutive duplicate user texts (the harness may log the same request twice) collapse.
    """
    out: list[tuple[int, dict]] = []
    u_idx = -1
    last_user = None
    pending: list[tuple[int, str]] = []

    for i, row in enumerate(rows):
        role = row.get("role")
        if role == "user":
            u_idx += 1
            ts = user_ts[u_idx] if 0 <= u_idx < len(user_ts) else None
            for ai, text in pending:  # flush previous turn's finals just before this turn
                out.append((ai, {"role": "assistant", "sender_id": HOST,
                                 "timestamp": (ts - 1) if ts else fallback_ms, "content": core.clip(text)}))
            pending = []
            text = clean_user(text_of(row.get("content")))
            if not text or text == last_user:
                continue
            last_user = text
            out.append((i, {"role": "user", "sender_id": core.owner(), "timestamp": ts or fallback_ms,
                            "content": core.clip(text)}))
        elif role == "assistant" and not row.get("tool_calls"):
            text = text_of(row.get("content")).strip()
            if text:
                pending.append((i, text))
    for ai, text in pending:
        out.append((ai, {"role": "assistant", "sender_id": HOST, "timestamp": fallback_ms,
                         "content": core.clip(text)}))
    out.sort(key=lambda x: x[0])
    return core._monotonic([m for _, m in out])


def assignment_timestamps(assign_text: str) -> list[int]:
    return [parse_ts(m) for m in ASSIGN_TS.findall(assign_text)]


def main_agent_dir(sessions_root: Path, sid: str) -> Path:
    """Locate the main agent directory for a Doubao Work session (assignment.md Role: main)."""
    base = sessions_root / sid / "agents"
    if not base.is_dir():
        raise FileNotFoundError(f"会话目录不存在: {base}")
    for d in sorted(base.iterdir()):
        assign = d / "system" / "assignment.md"
        if assign.is_file() and re.search(r"^- Role:\s*main\s*$", assign.read_text(encoding="utf-8"), re.M):
            return d
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
    ts = user_ts[0] / 1000 if user_ts else time.time()
    day = datetime.fromtimestamp(ts, core.CST)
    return f"dw_{day:%Y%m%d}_{core.slug(first) or 'session'}_{sid}"[:128]


def read_session(sessions_root: Path, sid: str) -> dict:
    """Read assignment + complete trajectory lines for one session.

    Returns parsed rows, their byte offsets (start of each line) and the file length up to
    the final newline. A half-written trailing line is left for the next run.
    """
    adir = main_agent_dir(sessions_root, sid)
    traj = adir / "system" / "trajectory.jsonl"
    assign = (adir / "system" / "assignment.md").read_text(encoding="utf-8")
    raw = traj.read_bytes()
    end = raw.rfind(b"\n") + 1
    rows, offsets = [], []
    pos = 0
    for line in raw[:end].splitlines(keepends=True):
        try:
            rows.append(json.loads(line))
            offsets.append(pos)
        except json.JSONDecodeError:
            pass
        pos += len(line)
    return {"agent_dir": adir, "trajectory": traj, "rows": rows, "offsets": offsets, "length": end,
            "user_ts": assignment_timestamps(assign),
            # First-row fingerprint: appending later turns must not invalidate it.
            "head_hash": hashlib.sha256(
                json.dumps(rows[0], ensure_ascii=False, sort_keys=True).encode()).hexdigest() if rows else ""}


def ingest_session(sid: str, *, sessions_root: Path | None = None, dry_run: bool = False,
                   flush: bool = False, since_bytes: int | None = None) -> dict:
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
    core.SESS_DIR.mkdir(parents=True, exist_ok=True)
    sp = core.SESS_DIR / f"doubao_{sid}.json"
    with open(sp.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = core.load_state(sp)
        offset = int(st.get("offset", 0)) if since_bytes is None else since_bytes
        head_hash = st.get("head_hash")
        if head_hash and (head_hash != data["head_hash"] or offset > length):
            raise RuntimeError("trajectory 已被改写或截断（offset/首行不匹配）；为避免重复入库已停止，请人工核对")
        new_rows = [(i, row) for i, row in enumerate(rows) if offsets[i] >= offset]
        msgs = messages_doubao([r for _, r in new_rows], _aligned_ts(data["user_ts"], new_rows, rows),
                               fallback) if new_rows else []
        result = {"session": sid, "everos_session_id":
                  st.get("everos_session_id") or session_name(sid, rows, data["user_ts"]),
                  "new_messages": len(msgs), "roles": [m["role"] for m in msgs], "dry_run": dry_run,
                  "offset_before": offset}
        if dry_run:
            result["preview"] = [{"role": m["role"], "timestamp": m["timestamp"], "chars": len(m["content"]),
                                  "head": m["content"][:80]} for m in msgs]
            return result
        core.ensure_writable()
        if not head_hash:
            st.update(claude_session_id=sid, host=HOST, cwd=str(Path.home() / "Doubao" / "chats" / sid),
                      transcript_path=str(data["trajectory"]), everos_session_id=result["everos_session_id"],
                      head_hash=data["head_hash"])
        new_offset = offsets[new_rows[-1][0]] + 1 if new_rows else (min(offset, length) if offset else 0)
        if msgs:
            res = core.post_messages(st["everos_session_id"], msgs)
            st.update(pending=True, api=core.version(), last_write_ms=int(time.time() * 1000),
                      last_write_status=res, writes=st.get("writes", 0) + 1,
                      messages=st.get("messages", 0) + len(msgs))
            core.log(f"doubao_work add {st['everos_session_id']} +{len(msgs)} -> {res}")
            result["add"] = res
            new_offset = length  # messages may include finals read before this byte boundary; advance to read end
        st["offset"] = new_offset
        core.save_state(sp, st)
        if flush and st.get("pending"):
            res = core.flush(st["everos_session_id"], ver=st.get("api"))
            st.update(pending=False, last_flush_ms=int(time.time() * 1000), last_flush_status=res)
            core.save_state(sp, st)
            core.log(f"doubao_work flush {st['everos_session_id']} -> {res}")
            result["flush"] = res
        result["offset_after"] = st["offset"]
    try:
        core.sweep()  # rate-limited inside; failures never undo a successful add
    except Exception as exc:
        core.log(f"doubao_work sweep failed: {exc!r}")
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
            core.log(f"doubao_work ingest failed {sid}: {exc!r}")
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
    args = parser.parse_args(argv)
    if core.cfg().get("EVEROS_WRITE") == "0" and not args.dry_run:
        print(json.dumps({"error": "EverOS 写入已关闭（EVEROS_WRITE=0）"}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        if args.all:
            return _ingest_all(args)
        sid = current_session_id() if args.current else args.session
        print(json.dumps(ingest_session(sid, sessions_root=Path(args.sessions_root), dry_run=args.dry_run,
                                        flush=args.flush), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        core.log(f"doubao_work ingest failed: {exc!r}")
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
