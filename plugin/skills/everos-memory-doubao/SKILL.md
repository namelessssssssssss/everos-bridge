---
name: everos-memory
description: Doubao Work（豆包办公）harness 上的 EverOS 长期记忆读写入口。harness 不支持 agent 注册 MCP，也没有生命周期 hook；需要在会话开始读用户画像、每条提问前检索相关记忆、任务交付/纠错/偏好/决策/排障闭环时写入结构化记录、每轮结束前把对话原文入库时使用。Use for recalling the user's long-term memory on every prompt and for storing durable facts, decisions and troubleshooting practices on Doubao Work.
---

# EverOS 记忆 · Doubao Work（豆包办公）

Doubao Work 没有 MCP 注册入口和生命周期 hook，读写都通过本仓的两个命令完成，规则以
`~/.everos/PROTOCOL.md`（软链到本仓 `PROTOCOL.md`）为唯一依据。记忆只是背景参考，与当前
对话冲突时以当前对话为准。

首次使用按 `docs/doubao-integration.md` 安装：`install.sh` 会把 `everos-mcp` 和
`everos-doubao-ingest` 链到 `~/.local/bin`，并在检测到用户 skill 目录时同步本 skill。

```bash
bash <本skill目录>/check.sh   # 环境自检；缺什么会给修复提示
```

## 读（L3，没有 hook，每轮主动执行）

- 首轮先读画像：`everos-mcp memory_profile '{}'`
- 每条提问都检索：`everos-mcp memory_search '{"query":"<按当前提问改写>","kind":"all","top_k":5}'`
  - 问“以前怎么做的”用 `"kind":"practices"`；普通召回覆盖不到的隐含/复杂历史问题，可加
    `"method":"agentic"`（最长约 180 秒，参数校验与超时规则同其他宿主）。
- 同一会话同一问题不重复检索。

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
- 会话记录格式是 harness 内部布局（`~/.doubao/agent_mode/workspace/.sessions/<sid>/agents/
  <main>/system/`），harness 改版可能需要同步更新 `plugin/doubao/doubao.py`。

## 交付自检

写了记忆，在回复里列出写了什么；没写，结尾输出「记忆写入：无」。不要在回复里复述 API key。
