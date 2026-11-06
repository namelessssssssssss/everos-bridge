# 使用指南

[English](usage.md) · 简体中文

[返回 README](../README.zh-CN.md)

本指南介绍 EverOS Bridge 的日常使用。[PROTOCOL.md](../PROTOCOL.md) 仍是读写规则的唯一依据；详细的宿主和服务说明目前为中文。

标注 **0.10.0+** 的功能需要对应的 Bridge 版本。更早的安装仍沿用之前的自动抽取行为。

## 记忆工具

你可以用日常语言让 agent 保存决策、查找过去的工作，或修正一条画像。接入的 agent 可以使用以下共享工具：

| 工具 | 用途 |
| --- | --- |
| `memory_profile` | 读取用户画像 |
| `memory_search` / `memory_open` | 查找记忆，再展开检索结果的已保存快照 |
| `memory_recent` | 查看最近的记忆条目 |
| `memory_context` | 在本机保留一个会话的任务线索和检索上下文 |
| `memory_remember` | 记录事实、决策、偏好和纠错 |
| `memory_record_practice` | 记录包含至少三个真实步骤的排障轨迹 |
| `memory_flush`（0.10.0+） | 任务需要时，显式请求抽取一个已保存的会话 |
| `memory_fix_profile` | 根据条目 ID 和理由修正画像 |
| `memory_rules` | 读取已安装的规则及其变化 |

新写入默认使用 v2。只有配置为 v2 且 `EVEROS_READ_V1=1` 时，做法记录才同时写入 v1；关闭旧版访问也会关闭这条双写。写入确认和抽取行为见[理解写入结果](#理解写入结果)。

## 配置

默认数据目录是 `~/.everos`，可设置 `EVEROS_HOME` 使用其他位置。凭据从该目录的 `config.env` 读取，非空的环境变量优先。

```dotenv
EVEROS_API_BASE=https://api.evermind.ai
EVEROS_USER_ID=your-memory-space
EVEROS_API_KEY=your-api-key
EVEROS_API_VERSION=v2
```

在本机替换占位值。为了兼容旧版 Bridge，`config.env` 中使用不带引号的值。Unix 安装器创建该文件时使用 `0600` 权限；原生 Windows 使用 ACL 保护。不要提交此文件，也不要把内容贴到对话或 issue 中。

| 选项 | 默认值 | 用途 |
| --- | --- | --- |
| `EVEROS_API_VERSION` | `v2` | API 版本；保留 `v1` 用于兼容 |
| `EVEROS_READ_V1` | 关闭 | 设置为 `1`，合并旧版 v1 检索，并在 v2 模式下启用做法双写 |
| `EVEROS_AGENT_ID` | `<user_id>_agent` | 各宿主共用的助手身份 |
| `EVEROS_AGENTIC_TIMEOUT` | `180` | agentic 检索的总预算，单位为秒 |
| `EVEROS_CASE_WAIT` | `30` | 做法 case 回读的最长等待秒数；从 0.10.0 起，只有 v2 add 返回 `extracted` 才轮询；`0` 关闭回读 |
| `EVEROS_INJECT` | 开启 | 设置为 `0`，关闭自动注入 |
| `EVEROS_WRITE` | 开启 | 设置为 `0`，关闭 Bridge 的全部写入 |

加入不带引号的 `EVEROS_WRITE=0`，可关闭采集、MCP 写工具、画像编辑、flush 和本地记忆同步。`EVEROS_INJECT=0` 不会关闭手动检索。

## 终端检索

完整安装会在 `~/.local/bin` 中创建 `everos`。只需要命令入口时，运行 `./install.sh --cli-only`，或加上 `--cli-only --dry-run` 先预览。这不会配置宿主、修改凭据或调用 API。已有冲突命令文件会被保留并报告。

```bash
everos search "What did we decide about the cache?"
everos search "How did we diagnose the timeout?" --kind practices --top-k 10
everos search "Earlier migration constraints" --method agentic --timeout 240
everos search "That earlier timeout" --context "Now checking the retry policy"
```

| 参数 | 可用值与行为 |
| --- | --- |
| Query | 1–500 个字符；包含空格的问题用引号括起 |
| `--kind` | `all`（默认）、`episodes` 或 `practices` |
| `--top-k` | 默认 `5`；接受 `1–100`，或用 `-1` 交给服务端决定 |
| `--method` | `hybrid`（CLI 默认）或 `agentic` |
| `--timeout` | 正有限秒数；覆盖配置的预算 |
| `--context` | 额外的任务线索，最多 500 个字符 |
| `--session-id` | 可选的会话标识，用于本机检索上下文 |

`top-k` 分别作用于每类记忆，因此合并结果可能超过该数量。用 `everos open em_... --offset 0 --limit 4000` 展开返回的 ID，将 `em_...` 替换为实际 ID，并沿返回的续读位置继续。这里读取的是检索结果在本机保存的快照，不是完整原始对话，也不保证内容仍是当前事实。换设备后，应重新检索以取得本机快照 ID。

Search 和 open 不写入远端记忆。Search 可以保存本机结果快照，但不会更新 hook 的会话去重账本。

| 退出码 | 含义 |
| --- | --- |
| `0` | 已完成，包括未命中或部分成功；应留意结果不完整的提示 |
| `1` | 配置或操作失败，包括所有检索来源均失败 |
| `2` | 参数错误 |
| `130` | 被用户中断 |

`everos --help` 和 `everos search --help` 不需要凭据或网络访问。

## 按需抽取（0.10.0+）

对于 v2，保存消息和抽取记忆是两个步骤。Bridge 确认每次 add 后，将抽取交给 EverOS，除非 agent 判断当前需要最新的已保存内容。适合显式抽取的时机包括交接、流程完成、话题切换，或依赖最新消息的搜索。[EverOS Cloud 会在两小时没有新消息后处理闲置会话](https://docs.evermind.ai/cookbook/quickstart)；不能假定每种自托管或旧版服务都具备这一行为。

在 agent 中使用 `memory_flush`，或在终端使用 `everos flush`。需要提供非空理由，并且**只能选择一个**目标：

| MCP 参数 / CLI 选项 | 目标 |
| --- | --- |
| `session_id` / `--session-id` | 结构化写入回执返回的 EverOS 会话 ID |
| `conversation_id` / `--conversation-id` | 提问 hook 显示的宿主原生会话 ID；需要已有本机采集账本 |

原生 conversation ID 与 EverOS session ID 不同。尚未采集任何写入的会话，没有可供此命令抽取的内容。Flush 不会采集或上传尚未送出的最终回复。需要立即交接时，先用 `memory_remember` 保存结论，再按需对返回的目标执行 flush。

MCP 参数示例，使用写入回执中的实际 ID：

```json
{
  "session_id": "ID_FROM_WRITE_RECEIPT",
  "reason": "The handoff needs the latest saved decision",
  "api_version": "v2"
}
```

等价的终端示例：

```bash
everos flush --session-id ID_FROM_WRITE_RECEIPT \
  --reason "The handoff needs the latest saved decision" --api-version v2

everos flush --conversation-id NATIVE_SESSION_ID \
  --reason "The completed workflow is ready for handoff"
```

`--api-version v1|v2` 可省略：直接使用 session ID 时，默认采用配置的 API 版本；使用 conversation ID 时，采用采集账本中的版本。版本切换后，可以显式指定旧版本，处理保留下来的旧目标。`--kind episodes|practices` 默认是 `episodes`；v1 的 agent-memory 入口使用 `practices`。Conversation 目标只支持 `episodes`。MCP 中对应的选项是 `api_version` 和 `kind`。

在 0.10.0+ 中，v2 的 remember/practice 写入、对话生命周期事件、MiMo 采集、本地记忆同步，以及普通或强制 sweep 都不会自动 flush。SessionEnd 和 PreCompact 仍会补采集剩余消息。旧版 v1 的自动 flush 行为保留，包括版本切换前对旧缓冲的确认。Sweep 仍会同步本地记忆、恢复待确认的 v1 flush，但不会强制抽取 v2，也不会重放尚未发送的对话。

已确认的 add 可以推进采集进度。Flush 失败时保留待确认状态，不会重新发送该次已确认的 add。本机 pending 状态表示尚未取得本机 flush 确认，不能据此断言 Cloud 中仍有未处理的消息。Flush 成功也不保证立即可检索或一定生成做法 case。所有显式 flush 都受 `EVEROS_WRITE=0` 和配额控制。升级后，重连 MCP 或新开 agent 会话，才能加载新工具和指令。

## 检索超时

Hook 和 MCP `memory_search` 首次使用 `agentic`；独立 CLI 默认使用 `hybrid`。失败、超时或未命中时，可在同一截止时间内按来源回退到 hybrid。旧版 v1 查询始终使用 hybrid。

Agentic 的默认总预算是 180 秒。优先级依次为单次 MCP `timeout` / CLI `--timeout`、环境变量 `EVEROS_AGENTIC_TIMEOUT`、`config.env`、默认值。

宿主的外层超时必须大于该预算。增大长期预算配置后，重跑安装器以更新支持的宿主限制。Claude Code、Codex 插件和云端静态 hook 定义默认是 190 秒；将它们的检索预算调到 180 秒以上时，还需要增大 `UserPromptSubmit` 超时并重新审阅更新后的 hook。各宿主限制见[协议](../PROTOCOL.md)。

所有来源均发生技术失败时，会明确报错，不会表述为“没有记忆”。部分成功时保留可用结果，并提示缺失的来源。

## 各宿主接入

| 宿主 | 说明 |
| --- | --- |
| Codex | 插件安装会复制文件。更新后需重跑安装器，新开会话，并审阅当前 hook 定义。 |
| Kimi Code | `PATH` 中找不到 `kimi` 时，安装器也会检查 `~/.kimi-code/bin/kimi`。画像在首次提问时注入。 |
| Kimi Work 桌面端 | App 升级可能覆盖 runtime 配置。重跑 `install.sh`，或在 Windows 上运行 `install.ps1 -KimiDesktop`，再新开会话。 |
| TraeX | 生命周期 hook 需要 0.209.1 或更新版本。用 `./install.sh --only trae` 只更新 TraeX，再通过 `/hooks` 审阅 hook。[详情](traex-integration.md)。 |
| MiMoCode / MiMo Desktop | 使用 Plugin API，而非 `hooks.json`。安装器合并 MCP、指令和插件设置，新会话生效。 |
| Claude Desktop | 安装前退出 App，或通过其设置配置 MCP。运行中的 App 可能覆盖外部配置改动。 |
| 原生 Windows | `install.ps1` 支持 Codex；加上 `-KimiDesktop` 后支持 Kimi Work。其他 Windows 宿主不在此安装器的覆盖范围内。[详情](windows-integration.md)。 |
| Doubao Work 云端 | 无原生 MCP 注册或 hook。用户 skill 通过 `everos-mcp` 调用共享 MCP 服务，并调用 `everos-doubao-ingest`；是否执行取决于 agent。[详情](doubao-integration.md)。 |

链接的宿主指南为中文。新增接入应遵循[接入清单](agent-onboarding.md)，分别验证工具、指令、注入和采集。

### Claude Code on the web

云端环境需要单独接入；本地安装的插件不会自动带过去。

1. 允许访问 `api.evermind.ai`。环境支持时，配置带 `Authorization: Bearer <key>` 请求头的网络密钥，让 key 留在虚拟机之外；否则通过环境的密钥机制提供 `EVEROS_API_KEY`。
2. 设置 `EVEROS_USER_ID`。仅在需要旧版检索时加入 `EVEROS_READ_V1=1`。默认仓库公开，无需 GitHub token。使用 fork 时，设置 `EVEROS_BRIDGE_REPO=owner/repo`；私有 fork 还需提供有该仓库读取权限的 `GH_TOKEN`。
3. 将 [`cloud/setup.sh`](../cloud/setup.sh) 用作 setup script，它会把 Bridge 安装到 `/opt/everos-bridge`。
4. 将 [`cloud/settings.snippet.json`](../cloud/settings.snippet.json) 合并到个人仓库的 `.claude/settings.json`，将 [`cloud/mcp.snippet.json`](../cloud/mcp.snippet.json) 合并到 `.mcp.json`。保留无关设置，不把个人接线放入共享团队仓库。
5. 新开一个云端会话，验证召回和采集。Hook 模板通过 `CLAUDE_CODE_REMOTE` 和已安装文件判断是否执行。

## 更新

macOS / Linux：

```bash
git -C ~/.everos-bridge pull --ff-only
~/.everos-bridge/install.sh
python3 ~/.everos-bridge/plugin/everos/hook.py --doctor
```

Windows 上更新源码 checkout 后，重新运行对应的 `install.ps1` 命令。重连 MCP 或新开会话以加载更新的工具；出现提示时，审阅更新后的 hook 定义。保留仍被旧会话使用的 checkout 和旧版插件。

安装器支持自定义 checkout 路径。正常安装会在修改配置前备份；`--dry-run` 只预览接入动作，不修改配置或调用 API。

## 排查问题

### 检查安装

```bash
python3 ~/.everos-bridge/plugin/everos/hook.py --doctor
```

Doctor 会检查连通性。可以在 `~/.everos/state/hook.log` 中查看运行错误，但报告中不要包含私有文本和凭据。新会话注入及真实的写入后回读，需要与配置检查分别验证。

Bridge 0.9.3 及更早版本中，若安装成功但鉴权失败，应检查 `config.env` 中是否有带引号的值。这些版本的 runtime 会保留外层引号；使用不带引号的值可兼容旧版。

### 从硬配额错误恢复

```bash
everos quota status
everos quota retry --resource mcu
# For retrieval quota instead:
everos quota retry --resource retrieval
```

`status` 读取本机熔断状态，不查询云端余额。补充额度后，`retry` 允许下一次相关请求探测是否恢复。命令本身不发送网络请求，也不表示额度已经恢复。不手动 retry 时，默认暂停 15 分钟后允许一次探测。

MCU 与检索配额相互独立，因此 MCU 拒绝不会关闭读取。普通速率限制响应与明确的硬配额错误分别处理。

采集失败会保留来源位置，不会把消息标记为已发送。从 **0.10.0** 起，sweep 只对待确认的旧版 v1 会话执行 flush，并同步本地记忆；v2 由服务端处理，或通过[指定目标显式 flush](#按需抽取0100) 抽取。Sweep 不会重放尚未发送的对话。恢复需要从保留的游标进行有界重放，并回读核对。

### 理解写入结果

| 结果 | 含义 |
| --- | --- |
| `accumulated` | 消息已被接收到缓冲区，尚待抽取 |
| `extracted` | 已完成抽取；搜索可见性和做法 case 生成仍需分别核对 |
| `no_extraction` | 此次 flush 没有抽取出新内容；单凭这个状态不能认定失败或数据缺失 |

做法记录至少需要三个真实步骤。应记录实际发生的失败尝试、意外发现和用户纠正，不要为了满足门槛编造步骤。从 **0.10.0** 起，只有 v2 add 返回 `extracted` 时，才执行原有的有界 case 回读。已接收但仍在缓冲的 add 会提示尚待抽取，暂不轮询 case，也不承诺已经存在 case 或可检索的记忆。没有 case 不一定代表写入失败。在 v2 模式下，只有 `EVEROS_READ_V1=1` 才启用向 v1 的做法双写。

服务的限制和历史实测见 [EverOS 服务说明](everos-notes.md)。本指南描述当前行为，不改变 Bridge 的抽取策略。

## 开发维护

如果 agent 在运行时加载当前 checkout，开发时应使用独立 Git worktree。

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
```

这些测试使用临时配置、模拟宿主和模拟 API，不需要真实凭据，也不会写入远端记忆。原生 Windows 测试位于 `windows/tests`，在独立的 [CI workflow](../.github/workflows/windows.yml) 中运行。

报告缺陷时，请提供 Bridge commit/版本、宿主和操作系统、复现步骤以及脱敏后的错误。不要包含 API key 或私有对话。宿主适配应在真实的新会话中分别验证召回和采集；仅离线测试不能证明宿主已加载并信任接入。

从 [Agent 指令](../AGENTS.md)和[接入清单](agent-onboarding.md)（中文）开始。核心代码在 [`plugin/everos/`](../plugin/everos/)，安装代码在 [`scripts/`](../scripts/) 和 [`windows/`](../windows/)，云端模板在 [`cloud/`](../cloud/)。设计背景见[方法论](methodology.md)，受管指令的更新见[规则同步](rule-sync.md)。
