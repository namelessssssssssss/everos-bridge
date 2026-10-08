---
name: everos-memory
description: 用户跨 agent、跨设备共用的 EverOS 长期记忆。会话开始读用户画像，每条提问前按提问检索相关记忆；任务交付、纠错、偏好、决策、排障闭环时写入结构化记录；用户纠正画像时直接修改画像。口径是默认多写不少写。Use for recalling the user's long-term memory on every prompt and storing durable facts, decisions and troubleshooting practices.
---

# EverOS 记忆

完整规则见 everos-bridge 仓库的 `PROTOCOL.md`，接入步骤见 `AGENTS.md`，这里是执行摘要。

## 用哪条通道

1. **有 MCP 工具 `memory_*` 时优先用**：`memory_profile`、`memory_search`、`memory_recent`、`memory_remember`、`memory_record_practice`、`memory_fix_profile`。
2. **装了 hook 的宿主**（Claude Code、Codex、Kimi Code）：画像、每条提问检索、对话原文入库都自动完成，你只需要补结构化记录。上下文里已经有「EverOS 用户画像」「EverOS 相关记忆」时，就不用重复检索。
3. **两者都没有时**（例如云端会话）：用下面的 curl 模板直接调 v2 接口。

## 读

- 会话开始读画像；每条提问前都按提问检索一次。
- 问「以前怎么做的」时用 `kind=practices`。
- 记忆只是背景参考；和当前对话冲突时以当前对话为准。

## 写

- 能长期复用的都写：偏好、纠错、决策及理由、项目事实与进度、任务结果、调研结论、排障路径、工具用法、环境坑。凭据已授权可以入库，但回复里不复述原文。
- `memory_remember`：
  - content 写结论、做了什么、证据（文件:行、MR、commit、链接）；
  - user_said 放用户原话，不改写；
  - 同一话题沿用同一个 topic。
- 排障或工具用法闭环时，用 `memory_record_practice` 写一条轨迹：需求 → 每步动作与结果 → 结论。
- 用户纠正了画像里的偏好或背景，或者画像里的进度过期了：先 `memory_profile` 拿到 item_id，再 `memory_fix_profile`，并写明 reason。
- episode 只追加不删除；owner 明确要求才删。
- 交付自检：写了就在回复里列出写入内容；没写就在结尾输出「记忆写入：无」。

## curl 模板（v2，没有 MCP 工具时用）

- 配置来自环境变量或 `~/.everos/config.env`：`EVEROS_USER_ID`、`EVEROS_API_KEY`、`EVEROS_API_BASE`（默认 https://api.evermind.ai）。
- 云端会话如果配了网络密钥，去掉 Authorization 头即可，代理会自动注入。
- 每条消息都要带 `sender_id`：用户原话用 user_id；助手消息用 `<user_id>_agent`，原 agent 名写在 `sender_name`。

```bash
[ -f ~/.everos/config.env ] && . ~/.everos/config.env
B=${EVEROS_API_BASE:-https://api.evermind.ai}; U=$EVEROS_USER_ID; A=${EVEROS_AGENT_ID:-${U}_agent}; NOW=$(($(date +%s)*1000)); SID=cowork_$(date +%Y%m%d)_<topic>
H=(-H "Content-Type: application/json"); [ -n "$EVEROS_API_KEY" ] && H+=(-H "Authorization: Bearer $EVEROS_API_KEY")
curl -s -X POST "$B/api/v2/memory/get" "${H[@]}" -d '{"memory_type":"profile","user_id":"'$U'"}'
curl -s -X POST "$B/api/v2/memory/search" "${H[@]}" -d '{"query":"<问题>","user_id":"'$U'","method":"hybrid","top_k":5,"min_score":0.3}'
curl -s -X POST "$B/api/v2/memory/add" "${H[@]}" -d '{"session_id":"'$SID'","async_mode":false,"messages":[{"sender_id":"'$U'","role":"user","timestamp":'$NOW',"content":"<用户原话>"},{"sender_id":"'$A'","sender_name":"<agent 名>","role":"assistant","timestamp":'$((NOW+1))',"content":"<结论 + 动作 + 证据>"}]}'
```
