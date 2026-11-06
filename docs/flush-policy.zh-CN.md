# 按需抽取记忆

[English](flush-policy.md) · 简体中文

Bridge 先确认消息写入成功，再推进采集进度。对于 v2，默认由 EverOS 处理抽取；只有 agent 判断当前任务需要立即使用最新保存的内容时，才显式触发抽取。

在有实际需要的边界调用 flush，例如交接、流程完成、话题切换，或即将检索的内容依赖最新保存的消息。不要在每次写记忆后机械调用。[EverOS 的生产环境建议](https://docs.evermind.ai/cookbook/quickstart)不要求每轮 flush；Cloud 会在两小时没有新消息后处理闲置会话。这项 Cloud 行为并不保证适用于所有自托管服务或旧版本服务。

## 选择一个目标

MCP 工具 `memory_flush` 与 CLI 命令 `everos flush` 的行为相同。两者都要求填写非空的 `reason`，并且只能指定一个目标：

| 字段 | 含义 |
| --- | --- |
| `session_id` / `--session-id` | 结构化写入回执返回的 EverOS 会话 ID |
| `conversation_id` / `--conversation-id` | 提问 hook 中显示的宿主原生会话 ID；通过已有的本机采集账本解析 |

这两种 ID 不同。不要将宿主原生会话 UUID 当成 EverOS session ID 传入。尚未采集并写入消息的会话没有可供此命令抽取的内容。宿主尚未保存当前最终回复时，此命令不会先读取或上传它。如果交接需要立即使用某个结论，先用 `memory_remember` 保存结论，再在有必要时使用回执中的目标 ID 触发抽取。

MCP 参数示例；请将占位符替换为实际写入回执中的 ID：

```json
{
  "session_id": "ID_FROM_WRITE_RECEIPT",
  "reason": "The handoff needs the latest saved decision",
  "api_version": "v2"
}
```

在终端中调用：

```bash
everos flush --session-id ID_FROM_WRITE_RECEIPT \
  --reason "The handoff needs the latest saved decision" --api-version v2

everos flush --conversation-id NATIVE_SESSION_ID \
  --reason "The completed workflow is ready for handoff"
```

`api_version` 为可选参数。直接传入 EverOS session ID 时，默认使用配置中的 API 版本；传入 conversation ID 时，默认使用其采集账本记录的版本。切换版本后，如需处理保留的旧目标，可通过 `api_version` 显式选择。`kind=practices` 用于选择 v1 做法记忆接口；普通记忆使用默认值 `episodes`。Conversation ID 只支持 `episodes`。

## 理解返回结果

- **Add 已确认：**服务端已接受提交的消息，采集游标可以前进。这本身不代表已经完成抽取，也不代表搜索已经可见。
- **Flush 已确认：**服务端返回了已识别的 `extracted` 或 `no_extraction` 状态，可以清除对应的本机待确认状态。
- **Case 或搜索可见性：**需要单独回读验证。Flush 响应不保证生成了做法 case，也不保证搜索立即能查到。

Flush 失败时保留 pending 状态，不重新发送已经确认的 add。对于已采集的会话，flush 与采集更新使用同一把会话锁，防止并发写入的待确认状态被清除。本机 `pending` 表示尚未记录本机 flush 确认；它并不是对 Cloud 缓冲区的实时查询，也不会仅因经过两小时就被清除。

对于做法记录，v2 add 返回 `extracted` 时，可以触发已有的有界 case 回读。消息已接受但仍在缓冲区时，回执会说明抽取待完成，不再花一个轮询周期等待 case。在适用的回读场景中，`EVEROS_CASE_WAIT` 控制等待时间。

## 自动路径与旧版兼容

- v2 的 `memory_remember`、做法记录、对话采集、MiMo 和本地记忆同步只确认 add，不自动 flush。
- SessionEnd 和 PreCompact 仍会采集剩余消息，但不自动 flush v2。
- 普通 sweep 和强制 sweep 都保留本地记忆同步与旧版 v1 恢复；两者都不自动 flush v2 会话。
- Doubao 已有的 `everos-doubao-ingest --current --flush` 继续作为显式指定目标的入口；默认采集路径不 flush v2。
- v1 保留兼容所需的 flush 行为。只有配置为 v2 且启用旧版 v1 访问时，做法记录才同时写入两个版本。
- 从 v1 切换到其他版本前，Bridge 仍要求确认旧缓冲区已处理。从 v2 切换时，则保留旧目标供显式处理，不自动强制抽取，也不丢弃其待确认元数据。

排查采集账本时，版本切换留下的待确认 v2 目标保存在 `deferred_flush["v2"]`。调用会话 flush 时指定 `api_version="v2"`，可以确认该目标，而不清除当前版本的待确认写入。新采集账本还会绑定后端、用户和 agent 身份。API base URL 仅在尾部斜杠上有差异时，会按请求构造方式视为同一后端。身份发生变化时，采集和会话 flush 会拒绝复用旧账本；sweep 会跳过身份不匹配的 v1 账本，并保留其 pending 状态。没有身份字段的旧账本继续按兼容路径处理。

包括 flush 在内的所有显式写入都遵守 `EVEROS_WRITE=0` 和配额控制。这些变更不改变可恢复采集所依赖的同步 add 确认机制，也不代表已经实测了 MCU 节省幅度或抽取质量提升。

更新 Bridge 后，重新连接 MCP 或打开新的 agent 会话，以加载 `memory_flush` 和新规则。已有进程可能仍然只暴露旧工具列表。详见[规则同步](rule-sync.md)和[协议](../PROTOCOL.md)。
