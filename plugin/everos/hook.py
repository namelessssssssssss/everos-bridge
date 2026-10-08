#!/usr/bin/env python3
"""生命周期 hook 入口：Claude Code、Codex（插件，都设置 CLAUDE_PLUGIN_ROOT）与 Kimi Code（config.toml [[hooks]]）共用。

  SessionStart      注入用户画像
  UserPromptSubmit  每条提问都按提问检索并注入相关记忆（得分门槛 + 会话内去重 + 预算上限）
  Stop              把新增的完整轮次写入 EverOS（转交后台子进程，立即返回）
  SessionEnd / PreCompact  写完剩余轮次并 flush

fail-open：任何异常只记日志并 exit 0，绝不阻塞会话。

手工用法：
  hook.py --doctor                      检查配置、连通性、状态目录
  hook.py --dry-run <transcript.jsonl>  只打印将写入的消息（自动识别 Claude Code / Codex 格式）
  hook.py --sweep                       补 flush 所有待抽取会话并全量同步本地记忆
  hook.py --sync-localmem               只同步 Claude Code 本地记忆
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

HEADER = "（EverOS 自动注入，仅供参考；与当前对话冲突时以当前为准）"


def emit(event: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                     ensure_ascii=False))


def on_prompt(payload: dict, kimi: bool = False) -> None:
    """每条提问都检索：v2 用得分门槛过滤，同一会话已注入过的不再重复，总预算与字数有上限。"""
    sid = str(payload.get("session_id") or "")
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return
    state_p = core.RECALL_DIR / f"{sid or 'nosession'}.json"
    st = core.load_state(state_p)
    first = not st.get("turns")
    budget = core._num("EVEROS_RECALL_TIMEOUT", 18)
    chars = int(core._num("EVEROS_RECALL_CHARS", 2000))
    blocks = []
    if kimi and first:  # Kimi 的 SessionStart 只通知不注入，画像放到首条提问一起给
        profile = core.profile_text()
        if profile:
            blocks.append(f"【EverOS 用户画像】{HEADER}\n{profile}")
    items = core.search_items(prompt, top_k=5, budget=budget,
                              v1_episodes=3 if core.substantial(prompt) else 0)
    seen = set(st.get("ids") or [])
    fresh = [it for it in items if it.get("id") not in seen]
    text = core.format_items(fresh, limit=chars)
    if text:
        blocks.append(f"【EverOS 相关记忆】{HEADER}\n{text}")
        shown = text.count("\n") + 1
        seen.update(str(it.get("id")) for it in fresh[:shown])
    st.update(turns=st.get("turns", 0) + 1, ids=sorted(seen)[-500:])
    core.save_state(state_p, st)
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
            print(core.doctor())
            return 0
        if args[:1] == ["--dry-run"]:
            entries, _ = core.read_new_entries(Path(args[1]), 0)
            host = core.detect_host(entries, args[1])
            parse = {"codex": core.messages_codex, "kimi": core.messages_kimi}.get(host, core.messages_claude)
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

        raw = sys.stdin.read()
        payload = json.loads(raw or "{}")
        event = str(payload.get("hook_event_name") or "")
        c = core.cfg()
        if not core.ready():
            return 0
        kimi = str(payload.get("client_type") or "").startswith("kimi")
        if event in ("SessionStart", "UserPromptSubmit"):
            if c.get("EVEROS_INJECT") == "0":
                return 0
            if event == "SessionStart" and not kimi:
                text = core.profile_text()
                if text:
                    emit("SessionStart", f"【EverOS 用户画像】{HEADER}\n{text}")
            elif event == "UserPromptSubmit":
                on_prompt(payload, kimi=kimi)
            return 0
        if event in ("Stop", "SessionEnd", "PreCompact") and c.get("EVEROS_WRITE") != "0":
            # 转交后台子进程，自己马上退出（SessionEnd 只有约 1.5 秒预算）
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True,
                                     env={**os.environ})
            child.stdin.write(raw.encode("utf-8"))
            child.stdin.close()
    except Exception as exc:  # fail-open：记忆读写永远不值得打断一次对话
        core.log(f"error {args[:1] or 'hook'}: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
