#!/usr/bin/env python3
"""生命周期 hook 入口：Claude Code、Codex（插件，都设置 CLAUDE_PLUGIN_ROOT）与 Kimi Code / Kimi Work 桌面端（config.toml [[hooks]]）共用。

  SessionStart      注入用户画像
  UserPromptSubmit  每条提问都按提问检索并注入相关记忆（得分门槛 + 会话内去重 + 预算上限）
  Stop              把新增的完整轮次写入 EverOS（转交后台子进程，立即返回）
  SessionEnd / PreCompact  写完剩余轮次并 flush

fail-open：生命周期事件的异常只记日志并 exit 0，绝不阻塞会话；--doctor 失败返回非零退出码。

手工用法：
  hook.py --doctor                      检查配置、连通性、状态目录
  hook.py --dry-run <transcript.jsonl>  只打印将写入的消息（自动识别 Claude Code / Codex 格式）
  hook.py --mimo-recall                 MiMoCode L3：stdin 收提问 JSON，stdout 输出注入文本
  hook.py --mimo-write                  MiMoCode L4：stdin 收轨迹 JSON 并写入 EverOS
  hook.py --sweep                       补 flush 所有待抽取会话并全量同步本地记忆
  hook.py --sync-localmem               只同步 Claude Code 本地记忆
"""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import filelock as fcntl  # noqa: E402
import recall  # noqa: E402

HEADER = "（EverOS 自动注入，仅供参考；与当前对话冲突时以当前为准）"
# hook 命令里可显式指定宿主（如 EVEROS_HOST=kimi_desktop python3 hook.py），覆盖 client_type 探测
HOST_OVERRIDE = os.environ.get("EVEROS_HOST", "")


def is_kimi_like(payload: dict) -> bool:
    """Kimi Code CLI 与 Kimi Work 桌面端都把 stdout 原文追加进上下文（SessionStart 只通知不注入）。"""
    if HOST_OVERRIDE in ("kimi", "kimi_desktop"):
        return True
    return str(payload.get("client_type") or "").startswith("kimi")


def emit(event: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                     ensure_ascii=False))


def build_prompt_blocks(sid: str, prompt: str, *, need_profile: bool = False) -> list[str]:
    """按提问检索并可选补画像；会话内去重。返回待注入文本块。"""
    state_p = recall.state_path(sid or "nosession")
    st = core.load_state(state_p)
    if st.get("identity") not in (None, recall.identity()):
        st = {}
    st["identity"] = recall.identity()
    st.setdefault("goal", prompt[:500])
    question = recall.query_text(prompt, recall.context_text(st) if st.get("clues") or st.get("stage") else "")
    budget = core.agentic_timeout()
    deadline = time.monotonic() + max(0.0, budget)
    chars = int(core._num("EVEROS_RECALL_CHARS", 2000))
    blocks = []
    need_profile = need_profile or bool(st.get("needs_profile")) or (
        HOST_OVERRIDE == "trae" and not st.get("profile_injected"))
    if need_profile:
        profile = core.profile_text(budget=min(core.READ_TIMEOUT, max(0.0, deadline - time.monotonic())))
        if profile:
            blocks.append(f"【EverOS 用户画像】{HEADER}\n{profile}")
            st.update(profile_injected=True, needs_profile=False)
    notices = []
    items = core.search_items(question, top_k=5, budget=max(0.0, deadline - time.monotonic()),
                              v1_episodes=3 if core.substantial(prompt) else 0, method="agentic", notices=notices)
    if notices:
        blocks.append("【EverOS 检索提示】\n" + "\n".join(notices))
    seen = set(st.get("ids") or [])
    fresh = recall.fresh_items(st, question, items)
    text, shown_ids = core.format_items_with_ids(fresh, limit=chars)
    if text:
        blocks.append(f"【EverOS 相关记忆】{HEADER}\n{text}")
        seen.update(shown_ids)
    recall.record_coverage(st, question, fresh, shown_ids)
    if not st.get("turns") or st.pop("needs_retrieval_policy", False):
        blocks.append(f"【EverOS 检索用法】session_id={sid}\n" + recall.POLICY)
    st.update(turns=st.get("turns", 0) + 1, ids=sorted(seen)[-500:])
    recall.save_state(state_p, st)
    return blocks


def on_prompt(payload: dict, kimi: bool = False) -> None:
    """每条提问先用 agentic 检索，失败或空结果按轨道回退；会话内去重、总预算与字数有上限。"""
    sid = str(payload.get("session_id") or "")
    prompt = str(payload.get("prompt") or "").strip()
    # 后台任务通知、系统提醒也会触发 UserPromptSubmit，不是用户提问，检索只会带来噪声
    if not prompt or prompt.startswith(core._SKIP_PREFIX + ("<system-reminder>",)):
        return
    state_p = recall.state_path(sid or "nosession")
    st = core.load_state(state_p)
    first = not st.get("turns") or st.get("identity") not in (None, recall.identity())
    need_profile = bool(kimi and first)  # Kimi 的 SessionStart 只通知不注入，画像放到首条提问一起给
    blocks = build_prompt_blocks(sid, prompt, need_profile=need_profile)
    if not blocks:
        return
    if kimi:
        print("\n\n".join(blocks))  # Kimi 把 stdout 原文追加进上下文
    else:
        emit("UserPromptSubmit", "\n\n".join(blocks))


def main() -> int:
    args = sys.argv[1:]
    try:
        if args[:1] == ["--doctor"]:
            report, healthy = core.doctor_result()
            print(report)
            return 0 if healthy else 1
        if args[:1] == ["--dry-run"]:
            entries, _ = core.read_new_entries(Path(args[1]), 0)
            host = core.detect_host(entries, args[1])
            parse = {"codex": core.messages_codex, "trae": core.messages_trae, "kimi": core.messages_kimi,
                     "kimi_desktop": core.messages_kimi}.get(host, core.messages_claude)
            msgs = parse(entries, host)
            print(core.session_name(entries, host, "", Path(args[1]).stem))
            for m in msgs:
                print(f"{m['role']:9s} {m['timestamp']} {len(m['content']):6d} | {m['content'][:90]!r}")
            return 0
        if args[:1] == ["--sweep"]:
            core.sweep(force=True)
            return 0
        if args[:1] == ["--sync-localmem"]:
            core.sync_local_memory()
            return 0
        if args[:1] == ["--worker"]:
            core.write_turns(json.loads(sys.stdin.read() or "{}"))
            return 0
        if args[:1] == ["--mimo-recall"]:
            # MiMoCode Plugin L3：stdin {"session_id","query","first","profile_only"} → stdout 注入文本
            data = json.loads(sys.stdin.read() or "{}")
            if core.cfg().get("EVEROS_INJECT") == "0":
                return 0
            sid = str(data.get("session_id") or "")
            if data.get("profile_only"):
                state_p = recall.state_path(sid or "nosession")
                st = core.load_state(state_p)
                if st.get("profile_injected") and not st.get("needs_profile"):
                    return 0
                profile = core.profile_text(budget=core._num("EVEROS_RECALL_TIMEOUT", 18))
                if profile:
                    st.update(profile_injected=True, needs_profile=False)
                    recall.save_state(state_p, st)
                    print(f"【EverOS 用户画像】{HEADER}\n{profile}")
                return 0
            prompt = str(data.get("query") or "").strip()
            if not prompt or prompt.startswith(core._SKIP_PREFIX + ("<system-reminder>",)):
                return 0
            blocks = build_prompt_blocks(sid, prompt, need_profile=bool(data.get("first")))
            if blocks:
                print("\n\n".join(blocks))
            return 0
        if args[:1] == ["--mimo-recover"]:
            data = json.loads(sys.stdin.read() or "{}")
            if core.cfg().get("EVEROS_INJECT") != "0":
                with recall.locked(str(data.get("session_id") or "nosession")) as state:
                    recall.reset_context(state)
                print(recall.POLICY)
            return 0
        if args[:1] == ["--mimo-write"]:
            # MiMoCode Plugin L4：stdin {"session_id","cwd","title","trajectory"}
            if core.cfg().get("EVEROS_WRITE") == "0":
                return 0
            core.write_mimo_turns(json.loads(sys.stdin.read() or "{}"))
            return 0

        raw = sys.stdin.read()
        payload = json.loads(raw or "{}")
        event = str(payload.get("hook_event_name") or "")
        sid0 = str(payload.get("session_id") or "")
        if sid0.startswith("ctitle-"):  # 桌面端标题生成侧会话：不检索、不采集
            return 0
        c = core.cfg()
        if not core.ready():
            return 0
        kimi = is_kimi_like(payload)
        if event in ("PreToolUse", "PostToolUse"):
            if c.get("EVEROS_INJECT") != "0":
                text = recall.tool_reminder(payload)
                if text:
                    emit(event, text)
            return 0
        if event in ("PreCompact", "PostCompact") or (event == "SessionStart" and
                str(payload.get("source") or "") in ("compact", "resume")):
            with recall.locked(sid0 or "nosession") as state:
                recall.reset_context(state)
                state["needs_retrieval_policy"] = True
        if event == "PostCompact" and HOST_OVERRIDE == "trae":
            # TraeX cannot inject from PostCompact; restore profile on the next prompt.
            state_p = recall.state_path(sid0 or "nosession")
            st = core.load_state(state_p)
            st.update(needs_profile=True, ids=[])
            recall.save_state(state_p, st)
            return 0
        if event in ("SessionStart", "UserPromptSubmit"):
            if c.get("EVEROS_INJECT") == "0":
                return 0
            if HOST_OVERRIDE == "trae":
                # A home-directory workspace can discover the same hooks.json
                # as both user and project source. Serialize read-side state as
                # well, so duplicate handlers neither call APIs nor inject twice.
                core.RECALL_DIR.mkdir(parents=True, exist_ok=True)
                state_p = recall.state_path(sid0 or "nosession")
                with open(state_p.with_suffix(".lock"), "a+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    st = core.load_state(state_p)
                    if event == "UserPromptSubmit":
                        turn = payload.get("turn_id")
                        key = str(turn) + ":" + hashlib.sha256(str(payload.get("prompt", "")).encode()).hexdigest()
                        if turn and st.get("last_prompt_key") == key:
                            return 0
                        on_prompt(payload)
                        if turn:
                            st = core.load_state(state_p)
                            st["last_prompt_key"] = key
                            recall.save_state(state_p, st)
                    else:
                        source = payload.get("source") or "startup"
                        if (st.get("last_profile_source") == source and
                                time.time() - st.get("last_profile_at", 0) < 2):
                            return 0
                        text = core.profile_text(budget=min(core.READ_TIMEOUT, core._num("EVEROS_RECALL_TIMEOUT", 18)))
                        if text:
                            emit("SessionStart", f"【EverOS 用户画像】{HEADER}\n{text}" + ("\n" + recall.POLICY if payload.get("source") in ("compact", "resume") else ""))
                            st.update(profile_injected=True, needs_profile=False,
                                      last_profile_source=source, last_profile_at=time.time())
                            recall.save_state(state_p, st)
                return 0
            if event == "SessionStart" and not kimi:
                text = core.profile_text(budget=min(core.READ_TIMEOUT, core._num("EVEROS_RECALL_TIMEOUT", 18)))
                if text:
                    emit("SessionStart", f"【EverOS 用户画像】{HEADER}\n{text}" + ("\n" + recall.POLICY if payload.get("source") in ("compact", "resume") else ""))
            elif event == "UserPromptSubmit":
                on_prompt(payload, kimi=kimi)
            return 0
        if event in ("Stop", "SessionEnd", "PreCompact") and c.get("EVEROS_WRITE") != "0":
            # 转交后台子进程，自己马上退出（SessionEnd 只有约 1.5 秒预算）
            launch = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                      if os.name == "nt" else {"start_new_session": True})
            child = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker"],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, **launch, env={**os.environ})
            child.stdin.write(raw.encode("utf-8"))
            child.stdin.close()
    except Exception as exc:  # fail-open：记忆读写永远不值得打断一次对话
        core.log(f"error {args[:1] or 'hook'}: {exc!r}")
        if args[:1] == ["--doctor"]:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
