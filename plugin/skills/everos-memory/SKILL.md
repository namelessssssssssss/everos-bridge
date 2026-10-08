---
name: everos-memory
description: 用户跨 agent、跨设备共用的 EverOS 长期记忆。会话开始读用户画像并按首条提问检索；用户提到「上次」「之前」「我说过」「记得吗」「按我习惯」时补检索；任务交付、纠错、偏好、决策、排障闭环时写入结构化记录。口径是默认多写不少写。Use for recalling the user's long-term memory and storing durable facts, decisions and troubleshooting practices.
---

# EverOS 记忆

完整规则见仓库根目录的 `PROTOCOL.md`，这里是执行摘要。

## 用哪条通道

1. **有 MCP 工具 `memory_*` 时优先用**：`memory_profile`、`memory_search`、`memory_recent`、`memory_remember`、`memory_record_practice`。
2. **Claude Code / Codex 装了插件时**：画像、首条检索、对话原文入库都由 hook 自动完成，你只需要补结构化记录。
3. **两者都没有时**（例如云端会话）：用下面的 curl 模板直接调接口。

## 读

- 会话开始：读画像，再用首条提问检索一次（hook 已经注入过的就不用重复）。
- 话题切换、提到过去、决策需要偏好支撑时，补检索。
- 问「以前怎么做的」时用 `kind=practices`。
- 记忆只是背景参考；和当前对话冲突时以当前对话为准。

## 写

- 能长期复用的都写：偏好、纠错、决策及理由、项目事实与进度、任务结果、调研结论、排障路径、工具用法、环境坑。凭据已授权可以入库，但回复里不复述原文。
- 用 `memory_remember` 写：
  - content 放结论、做了什么、证据（文件:行、MR、commit、链接）；
  - user_said 放用户原话，不改写；
  - 同一话题沿用同一个 topic。
- 排障或工具用法闭环时，再用 `memory_record_practice` 写一条轨迹：需求 → 每步动作与结果 → 结论。
- 只追加不删除；owner 明确说某条错了才删。
- 交付前自检：写了就在回复里列出写入内容；没有要写的就在结尾输出「记忆写入：无」。

## curl 模板（没有 MCP 工具时用）

- 配置来自环境变量或 `~/.everos/config.env`：`EVEROS_USER_ID`、`EVEROS_API_KEY`、`EVEROS_API_BASE`（默认 https://api.evermind.ai）。
- 云端会话如果配了网络密钥，去掉 Authorization 头即可，代理会自动注入。
- add 必带 `"async_mode": false`，然后 flush，否则写入会静默丢失。
- timestamp 用当前毫秒时间；补录旧事时把原始日期写进正文。

```bash
[ -f ~/.everos/config.env ] && . ~/.everos/config.env
B=${EVEROS_API_BASE:-https://api.evermind.ai}; U=$EVEROS_USER_ID; NOW=$(($(date +%s)*1000)); SID=cowork_$(date +%Y%m%d)_<topic>
H=(-H "Content-Type: application/json"); [ -n "$EVEROS_API_KEY" ] && H+=(-H "Authorization: Bearer $EVEROS_API_KEY")
curl -s -X POST "$B/api/v1/memories/get" "${H[@]}" -d '{"filters":{"user_id":"'$U'"},"memory_type":"profile"}'
curl -s -X POST "$B/api/v1/memories/search" "${H[@]}" -d '{"filters":{"user_id":"'$U'"},"query":"<问题>","method":"hybrid","top_k":5,"memory_types":["episodic_memory","agent_memory"]}'
curl -s -X POST "$B/api/v1/memories" "${H[@]}" -d '{"user_id":"'$U'","session_id":"'$SID'","async_mode":false,"messages":[{"role":"user","sender_id":"'$U'","timestamp":'$NOW',"content":"<用户原话>"},{"role":"assistant","sender_id":"agent","timestamp":'$((NOW+1))',"content":"<结论 + 动作 + 证据>"}]}'
curl -s -X POST "$B/api/v1/memories/flush" "${H[@]}" -d '{"user_id":"'$U'","session_id":"'$SID'"}'
```
