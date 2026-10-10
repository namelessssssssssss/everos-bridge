/**
 * EverOS Bridge · MiMoCode Plugin (L3 + L4)
 *
 * L3  session.pre / chat.message / experimental.chat.system.transform
 *     — 会话首条提问注入画像；每条提问检索相关记忆，以 synthetic text part 注入。
 * L4  session.post
 *     — 把本条 runLoop 的 trajectory 交给 hook.py --mimo-write 入库。
 *
 * fail-open：任何失败只写 stderr，绝不打断会话。
 */
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_HOOK = join(HERE, "..", "everos", "hook.py");
const HOST = "mimocode";

function hookPath(options) {
  return String(options?.hook || DEFAULT_HOOK);
}

function callHook(hook, args, payload) {
  return execFileSync("python3", [hook, ...args], {
    input: JSON.stringify(payload ?? {}),
    encoding: "utf8",
    timeout: 30000,
    env: { ...process.env, EVEROS_HOST: HOST },
    stdio: ["pipe", "pipe", "pipe"],
  });
}

function partId() {
  return `everos_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function textPart(sessionID, messageID, text) {
  return {
    id: partId(),
    sessionID,
    messageID: messageID || partId(),
    type: "text",
    text,
    synthetic: true,
    metadata: { source: "everos-bridge" },
  };
}

export const server = async (_ctx, options = {}) => {
  const hook = hookPath(options);
  const profileDone = new Set();
  const recallBusy = new Set();

  return {
    id: "everos",

    "session.pre": async () => {
      // 仅标记：画像放到首轮 chat.message / system.transform，避免空跑网络。
    },

    "experimental.chat.system.transform": async (input, output) => {
      const sid = input?.sessionID || "";
      if (!sid || profileDone.has(sid)) return;
      try {
        const text = callHook(hook, ["--mimo-recall"], {
          session_id: sid,
          query: "（会话开始，读取用户画像）",
          first: true,
          profile_only: true,
        }).trim();
        profileDone.add(sid);
        if (text) {
          output.system = [...(output.system || []), text];
        }
      } catch (err) {
        profileDone.add(sid);
        console.error(`[everos] profile inject failed: ${err}`);
      }
    },

    "chat.message": async (input, output) => {
      const sid = input?.sessionID || output?.message?.sessionID || "";
      const messageID = input?.messageID || output?.message?.id || partId();
      const parts = output?.parts;
      if (!sid || !Array.isArray(parts)) return;

      const query = parts
        .filter((p) => p && p.type === "text" && !p.synthetic && !p.ignored)
        .map((p) => String(p.text || ""))
        .join("\n")
        .trim();
      if (!query || query.startsWith("<system-reminder>") || recallBusy.has(sid)) return;

      const first = !profileDone.has(sid);
      recallBusy.add(sid);
      try {
        const text = callHook(hook, ["--mimo-recall"], {
          session_id: sid,
          query,
          first,
        }).trim();
        profileDone.add(sid);
        if (text) {
          parts.push(textPart(sid, messageID, text));
        }
      } catch (err) {
        profileDone.add(sid);
        console.error(`[everos] recall inject failed: ${err}`);
      } finally {
        recallBusy.delete(sid);
      }
    },

    "session.post": async (input) => {
      const sid = input?.sessionID || "";
      if (!sid) return;
      try {
        callHook(hook, ["--mimo-write"], {
          session_id: sid,
          cwd: _ctx?.directory || _ctx?.worktree || "",
          title: input?.task_id || "",
          outcome: input?.outcome || "completed",
          trajectory: input?.trajectory || [],
        });
      } catch (err) {
        console.error(`[everos] write failed: ${err}`);
      }
    },
  };
};

export default { id: "everos", server };
