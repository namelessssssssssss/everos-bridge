---
name: everos-memory
description: Doubao Work（豆包办公）harness 上的 EverOS 长期记忆读写入口。harness 不支持 agent 注册 MCP，也没有生命周期 hook；需要在会话开始读用户画像、每条提问首次用 agentic 检索相关记忆、任务中按证据缺口补查与展开、任务交付/纠错/偏好/决策/排障闭环时写入结构化记录、每轮结束前把对话原文入库时使用。Use for agentic recall of the user's long-term memory on every prompt and for storing durable facts, decisions and troubleshooting practices on Doubao Work.
---

# EverOS 记忆 · Doubao Work（豆包办公）

Doubao Work 没有 MCP 注册入口和生命周期 hook，读写都通过本仓的两个命令完成，规则以
`~/.everos/PROTOCOL.md`（软链到本仓 `PROTOCOL.md`）为唯一依据。记忆只是背景参考，不是当前
授权或实时状态；与当前对话冲突时以当前对话和已核实事实为准。

首次使用按 `docs/doubao-integration.md` 安装：`install.sh` 会把 `everos-mcp` 和
`everos-doubao-ingest` 链到 `~/.local/bin`，并在检测到用户 skill 目录时同步本 skill。

```bash
bash <本skill目录>/scripts/check.sh   # 环境自检；缺什么会给修复提示
everos-mcp list                       # 应列出 8 个 memory_* 工具
```

## 读（L3，没有 hook，每轮主动执行）

- 首轮先读画像：`everos-mcp memory_profile '{}'`
- **每条用户消息的首次检索必须用 agentic**（首轮和后续轮都一样），不要先跑 hybrid 再决定是否深查：
  `everos-mcp memory_search '{"query":"<按当前提问改写>","kind":"all","top_k":5,"method":"agentic","session_id":"dw_<会话id>"}'`
  - 省略 method 服务端也默认 agentic；显式写出便于自检。
  - 默认总预算 180 秒（含按轨道回退 hybrid），`everos-mcp` 客户端等 200 秒。**Bash 工具默认 120 秒
    超时会先打断它，调用时把 timeout 设为 240000**；输出里的降级/回退提示照常采信。
  - 工具直接返回 `http timeout`（不是 everos 输出）属于 harness 通道错误，不等于检索失败也不等于无
    记忆：改用 Bash 后台运行（run_in_background）再用 TaskOutput 取结果。
  - 问“以前怎么做的”用 `"kind":"practices"`。
- `<会话id>` 取主项目目录 `~/Doubao/chats/<会话id>` 的末段；同一会话内保持不变。
- 本轮同一问题已有充分证据时复用，不为凑调用次数重复检索。

## 任务中补查与展开

首次检索只覆盖当时的问题。制定方案、动手前，出现新报错/新实体/失败，准备向用户追问旧决定，
或上下文被压缩恢复时，先判断历史证据是否已覆盖；有缺口再定向补查，证据已在上下文里就复用。

- 补查：query 写当前要核实的问题和新标识，`context` 补现场线索（合计 ≤500 字）；补查可显式用
  `"method":"hybrid"` 换速度。
- 展开：结果行带 `id=em_…` 和 `truncated=true` 时，用
  `everos-mcp memory_open '{"memory_id":"em_…","offset":0,"limit":4000}'` 读正文，`next_offset`
  非空就接着读。返回的是本机服务端记忆快照，不是原始聊天，也不代表实时事实。
- 长任务转阶段：`everos-mcp memory_context '{"session_id":"dw_<会话id>","goal":"…","stage":"plan|act|new_evidence|recover","clues":"…"}'`（各字段 ≤500 字）。
  只更新本机上下文，不检索、不写 EverOS；压缩恢复用 `stage=recover` 清掉过期覆盖。
- 回顾近期在做什么：`everos-mcp memory_recent '{"days":3}'`。

| 场景 | 做法 |
| --- | --- |
| 出现新错误码 | 查“错误码＋组件”的历史排障，不因首轮已查就直接反复重试 |
| 要问用户旧决定 | 先查既有决定，摘要缺适用条件就 memory_open，不让用户重复已存信息 |
| 证据已齐 | 直接用已读证据 |
| 历史方案与现场冲突 | 标明历史日期/版本，核对当前事实；历史记忆不产生执行授权 |

## 写（结构化记录，关键节点主动调用）

- 结论、决策、纠错、偏好、项目事实：`everos-mcp memory_remember @<args.json>`，参数
  `{"topic","content","user_said"}`；同一话题沿用同一个 topic。
- 排障/工具用法闭环：`everos-mcp memory_record_practice @<args.json>`，至少 3 个真实步骤；
  失败的步骤标 `"outcome":"failed"`，意外标 `"unexpected"`，用户纠正原话填 `user_correction`。
- 画像写错或过期：先 `memory_profile` 拿 item_id，再 `memory_fix_profile`，每条写 reason。
- 参数含中文/引号时先把 JSON 写成文件再用 `@/abs/path.json`。
- `EVEROS_WRITE=0` 时所有写工具与采集都不能入库；`-32602` 参数校验失败表示请求没发出，不算已写入。

## 对话原文入库（L4，每轮最终回复前主动执行）

```bash
everos-doubao-ingest --all        # 增量写入接入时间点之后有更新的会话
everos-doubao-ingest --current    # 只写当前会话
```

只写用户原话（自动去掉 system-reminder）和助手最终回复；工具调用、工具结果、过程旁白不
上传。同一请求被 harness 记录两次会自动去重；账本只在写入被接受后推进，失败可重试。本轮
的最终回复要到下一轮执行时才会补写，这是无 hook 宿主的固有局限。

## 局限

- 没有平台级注入/采集钩子，读写是否每轮执行取决于 agent 是否按本 skill 行动，无法强制。
- agentic 首查最长约 3 分钟，是 owner 认可的质量换时间；Bash 超时没调大会被误判为失败。
- 会话记录格式是 harness 内部布局（`~/.doubao/agent_mode/workspace/.sessions/<sid>/agents/
  <main>/system/`），harness 改版可能需要同步更新 `plugin/doubao/doubao.py`。

## 交付自检

写了记忆，在回复里列出写了什么；没写，结尾输出「记忆写入：无」。不要在回复里复述 API key。
