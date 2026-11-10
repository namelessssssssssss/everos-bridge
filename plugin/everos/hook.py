#!/usr/bin/env python3
"""生命周期 hook 入口：Claude Code、Codex（插件，都设置 CLAUDE_PLUGIN_ROOT）与 Kimi Code / Kimi Work 桌面端（config.toml [[hooks]]）共用。

  SessionStart      注入用户画像
  UserPromptSubmit  每条提问都按提问检索并注入相关记忆（得分门槛 + 会话内去重 + 预算上限）
  Stop              把新增的完整轮次写入 EverOS（转交后台子进程，立即返回）
  SessionEnd / PreCompact  补写剩余轮次；v2 抽取由服务端或显式 memory_flush 处理

fail-open：生命周期事件的异常只记日志并 exit 0，绝不阻塞会话；--doctor 失败返回非零退出码。

手工用法：
  hook.py --doctor                      检查配置、连通性、状态目录
  hook.py --dry-run <transcript.jsonl>  只打印将写入的消息（自动识别 Claude Code / Codex 格式）
  hook.py --mimo-recall                 MiMoCode L3：stdin 收提问 JSON，stdout 输出注入文本
  hook.py --mimo-write                  MiMoCode L4：stdin 收轨迹 JSON 并写入 EverOS
  hook.py --sweep                       处理旧 v1 待抽取会话并同步本地记忆；不自动 flush v2
  hook.py --sync-localmem               只同步 Claude Code 本地记忆
"""
from __future__ import annotations

import json
import fcntl
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import recall  # noqa: E402

HEADER = "（EverOS 自动注入，仅供参考；与当前对话冲突时以当前为准）"
# hook 命令里可显式指定宿主（如 EVEROS_HOST=kimi_desktop python3 hook.py），覆盖 client_type 探测
HOST_OVERRIDE = os.environ.get("EVEROS_HOST", "")


def is_kimi_like(payload: dict) -> bool:
    """Kimi Code CLI 与 Kimi Work 桌面端都把 stdout 原文追加进上下文（SessionStart 只通知不注入）。"""
    if HOST_OVERRIDE in ("kimi", "kimi_desktop"):
        return True
    return str(payload.get("client_type") or "").startswith("kimi")


def is_antigravity(payload: dict) -> bool:
    if os.environ.get("EVEROS_HOST", HOST_OVERRIDE) == "antigravity":
        return True
    return any(k in payload for k in ("conversationId", "transcriptPath", "invocationNum", "terminationReason", "toolCall"))


def normalize_antigravity(payload: dict) -> dict:
    data = dict(payload)
    if isinstance(data.get("common"), dict):
        for k, v in data["common"].items():
            data.setdefault(k, v)
    for sub in ("preToolHookArgs", "postToolHookArgs", "preInvocationHookArgs",
                "postInvocationHookArgs", "stopHookArgs", "sessionStartHookArgs"):
        if isinstance(data.get(sub), dict):
            for k, v in data[sub].items():
                data.setdefault(k, v)
    event = str(data.get("hook_event_name") or os.environ.get("EVEROS_HOOK_EVENT") or "")
    if not event:
        if any(k in data for k in ("terminationReason", "fullyIdle", "finalModelOutput", "executionNum")):
            event = "Stop"
        elif "toolCall" in data:
            event = "PostToolUse" if ("error" in data or "result" in data) else "PreToolUse"
        elif any(k in data for k in ("modelOutput", "modelThinking")):
            event = "PostInvocation"
        elif any(k in data for k in ("invocationNum", "initialNumSteps", "lastUserInput")):
            event = "PreInvocation"
        elif "conversationId" in data:
            event = "SessionStart"
    data["hook_event_name"] = event
    if data.get("conversationId") and not data.get("session_id"):
        data["session_id"] = str(data["conversationId"])
    if data.get("transcriptPath") and not data.get("transcript_path"):
        data["transcript_path"] = str(data["transcriptPath"])
    if isinstance(data.get("workspacePaths"), list) and data["workspacePaths"] and not data.get("cwd"):
        data["cwd"] = str(data["workspacePaths"][0])
    if "lastUserInput" in data and not data.get("prompt"):
        data["prompt"] = core.clean_antigravity_text(data.get("lastUserInput")) or ""
    if isinstance(data.get("toolCall"), dict):
        tc = data["toolCall"]
        data.setdefault("tool_name", str(tc.get("name") or ""))
        data.setdefault("tool_input", tc.get("args") if isinstance(tc.get("args"), dict) else {})
    if ("error" in data or "result" in data) and "tool_response" not in data:
        data["tool_response"] = {"error": data.get("error", ""), "result": data.get("result", "")}
    return data


def antigravity_checkpoint_step(tpath_str: str, sid: str) -> int:
    tpath = core.antigravity_transcript(sid, tpath_str)
    if not tpath or not tpath.is_file():
        return -1
    entries, _ = core.read_new_entries(tpath, 0)
    steps = [int(o.get("step_index", -1)) for o in entries
             if isinstance(o, dict) and o.get("type") == "CHECKPOINT"]
    return max(steps) if steps else -1


def emit_antigravity(event: str, text: str = "") -> None:
    if event == "PreToolUse":
        print(json.dumps({"decision": "allow"}, ensure_ascii=False))
    elif event == "SessionStart" and text:
        print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]}, ensure_ascii=False))
    elif event in ("PreInvocation", "UserPromptSubmit", "PostInvocation") and text:
        print(json.dumps({"injectSteps": [{"ephemeralMessage": text}]}, ensure_ascii=False))
    else:
        print("{}")


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
        os.environ.get("EVEROS_HOST", HOST_OVERRIDE) in ("trae", "antigravity") and not st.get("profile_injected"))
    if need_profile:
        profile = core.profile_text(budget=max(0.0, min(core.READ_TIMEOUT, budget, deadline - time.monotonic())))
        if profile:
            blocks.append(f"【EverOS 用户画像】{HEADER}\n{profile}")
            st.update(profile_injected=True, needs_profile=False)
    notices = []
    items = core.search_items(question, top_k=5, budget=max(0.0, min(budget, deadline - time.monotonic())),
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
        blocks.append(f"【EverOS 检索用法】session_id={sid}\n" + recall.POLICY +
                      f"\n需要按需抽取本会话已采集的消息时，memory_flush 使用 conversation_id={sid} 并说明 reason。"
                      "不要把这个原生ID当成EverOS session_id；尚未采集的当前最终回复不在flush范围内。"
                      "仅在交接、流程完成、话题转移或立即检索有需要时调用，其他时候由服务端处理。")
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
            raw_path = Path(args[1])
            entries, _ = core.read_new_entries(raw_path, 0)
            host = core.detect_host(entries, args[1])
            if host == "antigravity":
                resolved = core.antigravity_transcript("", args[1])
                if resolved and resolved != raw_path:
                    entries, _ = core.read_new_entries(resolved, 0)
            parse = {"codex": core.messages_codex, "trae": core.messages_trae, "antigravity": core.messages_antigravity,
                     "kimi": core.messages_kimi, "kimi_desktop": core.messages_kimi}.get(host, core.messages_claude)
            msgs = parse(entries, host)
            sid_hint = raw_path.stem
            if host == "antigravity" and sid_hint in ("transcript", "transcript_full") and len(raw_path.parents) >= 3:
                sid_hint = raw_path.parents[2].name
            print(core.session_name(entries, host, "", sid_hint))
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
        antigravity = is_antigravity(payload)
        if antigravity:
            payload = normalize_antigravity(payload)
        event = str(payload.get("hook_event_name") or "")
        sid0 = str(payload.get("session_id") or "")
        if sid0.startswith("ctitle-"):  # 桌面端标题生成侧会话：不检索、不采集
            if antigravity:
                emit_antigravity(event)
            return 0
        c = core.cfg()
        if not core.ready():
            if antigravity:
                emit_antigravity(event)
            return 0
        kimi = is_kimi_like(payload)
        if event in ("PreToolUse", "PostToolUse"):
            if c.get("EVEROS_INJECT") != "0":
                text = recall.tool_reminder(payload)
                if antigravity:
                    if text:
                        with recall.locked(sid0 or "nosession") as state:
                            pending = list(state.get("pending_reminders") or [])
                            if text not in pending:
                                pending.append(text)
                            state["pending_reminders"] = pending[-8:]
                    emit_antigravity(event)
                    return 0
                if text:
                    emit(event, text)
            elif antigravity:
                emit_antigravity(event)
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
        if event in ("SessionStart", "UserPromptSubmit", "PreInvocation"):
            if c.get("EVEROS_INJECT") == "0":
                if antigravity:
                    emit_antigravity(event)
                return 0
            if antigravity:
                checkpoint = antigravity_checkpoint_step(str(payload.get("transcript_path") or ""), sid0)
                with recall.locked(sid0 or "nosession") as st:
                    prev_checkpoint = int(st.get("last_checkpoint_step", -1))
                    if checkpoint > prev_checkpoint:
                        if st.get("turns", 0) > 0:
                            recall.reset_context(st)
                            st["needs_profile"] = True
                            st["needs_retrieval_policy"] = True
                        st["last_checkpoint_step"] = checkpoint
                    if event == "SessionStart":
                        source = payload.get("source") or "startup"
                        if (st.get("last_profile_source") == source and
                                time.time() - st.get("last_profile_at", 0) < 2):
                            emit_antigravity("SessionStart")
                            return 0
                        text = core.profile_text(budget=min(core.READ_TIMEOUT, core._num("EVEROS_RECALL_TIMEOUT", 18)))
                        if text:
                            emit_antigravity(
                                "SessionStart",
                                f"【EverOS 用户画像】{HEADER}\n{text}"
                                + ("\n" + recall.POLICY if payload.get("source") in ("compact", "resume") else ""),
                            )
                            st.update(profile_injected=True, needs_profile=False,
                                      last_profile_source=source, last_profile_at=time.time())
                        else:
                            emit_antigravity("SessionStart")
                        return 0
                    pending_reminders = list(st.pop("pending_reminders", None) or [])
                    prompt = str(payload.get("prompt") or "").strip()
                    inv_num = int(payload.get("invocationNum") or 0)
                    turn_id = payload.get("turn_id")
                    if turn_id is None and "initialNumSteps" in payload:
                        turn_id = payload.get("initialNumSteps")
                    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest() if prompt else ""
                    turn_key = f"{turn_id}:{prompt_hash}" if turn_id is not None else ""
                    skip_prompt = bool(
                        not prompt
                        or prompt.startswith(core._SKIP_PREFIX + ("<system-reminder>",))
                        or (inv_num > 1 and st.get("last_prompt_hash") == prompt_hash)
                        or (turn_key and st.get("last_turn_key") == turn_key)
                    )
                blocks = []
                if not skip_prompt:
                    blocks = build_prompt_blocks(sid0, prompt)
                    with recall.locked(sid0 or "nosession") as st:
                        st["last_prompt_hash"] = prompt_hash
                        if turn_key:
                            st["last_turn_key"] = turn_key
                blocks.extend(pending_reminders)
                emit_antigravity(event, "\n\n".join(blocks))
                return 0
            if HOST_OVERRIDE == "trae":
                # A home-directory workspace can discover the same hooks.json
                # as both user and project source. Serialize read-side state as
                # well, so duplicate handlers neither call APIs nor inject twice.
                core.RECALL_DIR.mkdir(parents=True, exist_ok=True)
                state_p = recall.state_path(sid0 or "nosession")
                with open(state_p.with_suffix(".lock"), "w") as lock:
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
            worker_env = {**os.environ, "EVEROS_HOST": "antigravity"} if antigravity else {**os.environ}
            worker_input = json.dumps(payload, ensure_ascii=False) if antigravity else raw
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True,
                                     env=worker_env)
            child.stdin.write(worker_input.encode("utf-8"))
            child.stdin.close()
        if antigravity:
            emit_antigravity(event)
    except Exception as exc:  # fail-open：记忆读写永远不值得打断一次对话
        core.log_error(f"error {args[:1] or 'hook'}", exc)
        if args[:1] == ["--doctor"]:
            return 1
        if os.environ.get("EVEROS_HOST", HOST_OVERRIDE) == "antigravity":
            print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
