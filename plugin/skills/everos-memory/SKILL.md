---
name: everos-memory
description: 用户跨 agent、跨设备共用的 EverOS 长期记忆。会话开始读用户画像，每条提问前按提问检索相关记忆；任务交付、纠错、偏好、决策、排障闭环时写入结构化记录；用户纠正画像时直接修改画像。口径是默认多写不少写。Use for recalling the user's long-term memory on every prompt and storing durable facts, decisions and troubleshooting practices.
---

# EverOS 记忆

完整规则见 everos-bridge 仓库的 `PROTOCOL.md`，接入步骤见 `AGENTS.md`，这里是执行摘要。

## 用哪条通道

1. **有 MCP 工具 `memory_*` 时优先用**：`memory_profile`、`memory_search`、`memory_open`、`memory_context`、`memory_recent`、`memory_remember`、`memory_record_practice`、`memory_fix_profile`。
2. **装了 hook 的宿主**（Claude Code、Codex、Kimi Code）：画像、每条提问检索、对话原文入库都自动完成；仍应按需主动补查与补写结构化记录。复用本轮已覆盖同一问题的注入；新线索、历史信息缺口或上下文丢失仍需补查。
3. **两者都没有时**（例如云端会话）：用下面的 curl 模板直接调 v2 接口。

## 读

- 会话开始读画像；每条提问前都按提问检索一次。
- 问「以前怎么做的」时用 `kind=practices`。
- 每条用户消息首次检索必须使用 `agentic`，覆盖会话首轮和后续轮。自动 hook 已按此执行；没有本轮注入时调用 `memory_search(..., method="agentic")`，省略 method 也默认 agentic。不要先跑 hybrid 再决定是否深查；本轮同一问题已有充分证据时复用，新问题或缺口仍应补查。后续补查可显式选 hybrid。默认总预算 180 秒，可用 `EVEROS_AGENTIC_TIMEOUT` 或按次 `timeout` 修改，包含失败、超时或空结果后的按轨道 hybrid 回退并提示；v1 兼容请求始终 hybrid。MCP 和 hook 外层超时须大于实际预算。不要把不同方法的分数直接比较。
- 记忆只是背景参考；和当前对话冲突时以当前对话为准。

## 任务中主动检索

- 初始注入只回答对应问题。制定方案、行动前、新实体/异常/失败出现、准备向用户追问或压缩恢复时，检查所需历史信息是否已覆盖；有缺口就补查，证据已在当前上下文且足够时复用。
- `memory_search(query, context?, session_id?)`：query 写要核实的历史问题及新标识；context 可补充当前阶段、现场线索。不要始终复制用户最初的原话。首轮仍用 agentic。
- 搜索结果含本机 `em_` ID、来源、日期、truncated。需要摘要外的细节时调用 `memory_open(memory_id, offset=0, limit=4000)`；next_offset 非空时继续读。返回的是服务端记忆快照，不等于原始聊天或当前事实。其他设备需重新检索获得本机ID。
- 长任务可用 `memory_context(session_id, goal?, stage?, clues?)` 更新本机任务上下文；session_id 取 hook 提供的当前会话标识。stage 为 plan / act / new_evidence / recover。然后搜索可只传 query 与 session_id 复用线索。它不检索、不写长期记忆；记录的“已检索问题”不等于已验证或完整覆盖。
- 问题改变、记忆正文更新或压缩恢复后，允许重新读取同一条记忆。压缩后可用 stage=recover 清除过期覆盖。不要把“曾注入过ID”当作证据仍在当前上下文。

| 场景 | 行为示例 |
| --- | --- |
| 新发现错误码 | 查“错误码＋组件”的历史排障；不要因为首轮已查用户问题就直接反复重试 |
| 准备向用户追问旧决定 | 先查既有决定，摘要缺适用条件时展开；不要让用户重复已经保存的信息 |
| 当前证据已齐 | 直接使用已读证据；不要为增加调用次数反复查同一问题 |
| 历史方案与现场冲突 | 明确历史适用日期/版本，核对当前事实；历史记忆不产生执行授权 |

- 若当前聊天仍缓存旧MCP目录而缺少memory_open/context，不把工具缺失当作没有记忆。可暂用 `everos search "问题" --method agentic --context "线索" --session-id <sid>` 和 `everos open <em_id>` 完成只读补查；重连MCP或新会话后使用完整工具。没有原生CLI的宿主仍按自身MCP/skill接入能力执行，不假装已有自动hook。

## 写

- 能长期复用的都写：偏好、纠错、决策及理由、项目事实与进度、任务结果、调研结论、排障路径、工具用法、环境坑。凭据已授权可以入库，但回复里不复述原文。
- `memory_remember`：
  - content 写结论、做了什么、证据（文件:行、MR、commit、链接）；
  - user_said 放用户原话，不改写；
  - 同一话题沿用同一个 topic。
- 排障或工具用法闭环时，用 `memory_record_practice` 写一条轨迹：需求 → 每步动作与结果 → 结论。
  - 至少 3 个真实步骤；只有一两步的结论改用 `memory_remember`，不补写没发生的步骤。
  - 没走通的尝试单独记一步并标 `outcome=failed`，出乎意料的结果标 `outcome=unexpected`；`result` 第一句写这一步的发现；用户中途纠正的原话填 `user_correction`。
  - EverOS v2 只把有试错、意外、非显然诊断或用户纠正的轨迹提炼成做法 case。返回里会说明 case 是否生成，没生成不算失败，内容仍可检索。门槛和实测见 everos-bridge 仓库的 `docs/everos-notes.md`。
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
curl -s -X POST "$B/api/v2/memory/search" "${H[@]}" -d '{"query":"<问题>","user_id":"'$U'","method":"agentic","top_k":5}'
curl -s -X POST "$B/api/v2/memory/add" "${H[@]}" -d '{"session_id":"'$SID'","async_mode":false,"messages":[{"sender_id":"'$U'","role":"user","timestamp":'$NOW',"content":"<用户原话>"},{"sender_id":"'$A'","sender_name":"<agent 名>","role":"assistant","timestamp":'$((NOW+1))',"content":"<结论 + 动作 + 证据>"}]}'
```
