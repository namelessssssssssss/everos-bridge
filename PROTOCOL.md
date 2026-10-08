# EverOS 记忆协议（唯一依据）

> 2026-10-08 版。口径：能长期复用的信息都写、凭据可以入库、默认多写不少写。
> 各 agent 的接线（hook、MCP、skill）和周质检都以本文件为准；改规则先改这里。
> 官方文档索引：https://docs.evermind.ai/llms.txt

## 0. 定位

| 载体 | 放什么 |
| --- | --- |
| EverOS（`EVEROS_USER_ID` 指定的空间） | 所有 agent、所有设备共用的记忆主库：存全量，用的时候按需检索 |
| 各 agent 自带记忆（Claude Code 的 `~/.claude/projects/*/memory/`、Codex 的 `~/.codex/memories` 等） | 本机、本 agent 的速查。Claude Code 本地记忆由 hook 自动同步进 EverOS |
| 团队知识库 / 仓库文档 | 权威事实，有准入流程。EverOS 里的内容成熟后再按流程提炼进去，反向不成立 |

## 1. 接线：谁负责什么

| 能力 | Claude Code / Codex | 其他支持 MCP 的 agent（Claude Desktop、Cursor、Gemini、Kimi、Trae…） |
| --- | --- | --- |
| 会话开始读画像 | SessionStart hook 自动注入 | 调 `memory_profile` |
| 首条提问检索 | UserPromptSubmit hook 自动注入 | 调 `memory_search` |
| 对话原文入库 | Stop hook 每轮自动写 | 无自动采集，靠 1.3 的结构化记录 |
| 结构化记录、做法轨迹 | MCP 工具 `memory_remember` / `memory_record_practice` | 同左 |

- hook 和 MCP 服务都在插件目录 `plugin/everos/` 下（纯 Python 标准库），配置见 §5。
- 一个 agent 会话对应一个 EverOS session_id：Claude Code 是 `cc_<首日>_<标题>_<会话前8位>`，Codex 是 `cx_…`，跨天沿用。
- botmux 转来的飞书消息按 `<sender>` 标注真实发送者：owner 本人（`EVEROS_OWNER_ALIASES`）记为 owner，其他人记为 `feishu_<邮箱前缀>`。经 `botmux send` 发出的回复也会收进来。
- 单条超过 2 万字才截断，其余不过滤任何内容。

## 2. 写什么

能长期复用的都写，包括：
- 偏好、纠错、决策及理由；
- 项目事实与进度、任务结果；
- 调研结论、判断推理、背景、业务场景、服务架构；
- 排障路径、工具用法、环境坑；
- 凭据（owner 已授权入库，回复里不复述原文）。

## 3. 怎么写

### 3.1 自动（hook）

- add 带 `async_mode=false`，不逐轮 flush，交给 EverOS 按话题切分。
- SessionEnd、PreCompact、闲置 2 小时的会话由 hook 补 flush。

### 3.2 结构化记录（agent 在关键节点主动写）

- 时机：任务交付、用户纠错、偏好暴露、决策形成、排障闭环、调研结论、带时间的计划。
- 工具：`memory_remember`，参数 topic、content、user_said。
  - content 写结论、做了什么、证据（文件:行、MR、commit、链接、logid）；
  - user_said 放用户原话，不改写；
  - 同一话题沿用同一个 topic。
- 排障或工具用法闭环时，再调一次 `memory_record_practice`，参数 task、steps、conclusion。EverOS 会把它提炼成 agent_case，同类积累多了再提炼成 agent_skill。

### 3.3 已知坑（都是静默失败，不报错）

- **add 不带 `async_mode=false`**：接口返回 `queued`，紧跟着的 flush 会得到 `no_extraction`。之后这个 session 没有新消息，就永远不会被抽取。2026-10 排查出 101 个 session 因此丢失，已补救。
- **时间戳太旧会被丢弃**：timestamp 必须接近写入时刻。约 2 个月前的时间戳，接口返回 accumulated，但永远不抽取。补录旧事时，时间戳用当前时间，原始日期写进正文。
- **内容重复不入库**：EverOS 认为已有的内容会直接返回 `no_extraction`，这种属于正常。
- **核对写入**：先看 flush 是否返回 extracted，再用 get 按 session_id 查一遍。兜底命令 `python3 plugin/everos/hook.py --sweep`。

## 4. 读

- 会话开始：读画像，加一次首条提问检索（hook 自动做，或调 MCP 工具）。
- 话题切换、提到过去（上次 / 我说过 / 按我习惯 / 记得吗）、决策需要偏好支撑时，补检索。
- 问「以前怎么做的」时用 `kind=practices`（接口参数是 `memory_types:["agent_memory"]`）。
- 需要串联多条记忆的问题，可以直接调接口，用 `method=agentic`。
- 检索结果是背景参考；和当前对话冲突时以当前对话为准。

## 5. 配置与部署

- 配置：环境变量优先，其次 `$EVEROS_HOME/config.env`（默认 `~/.everos/config.env`，权限 0600，永不入库）。
  - 必填：`EVEROS_USER_ID`、`EVEROS_API_KEY`；
  - 可选：`EVEROS_API_BASE`、`EVEROS_OWNER_ALIASES`、`EVEROS_INJECT=0`（关读侧注入）、`EVEROS_WRITE=0`（关写侧）。
- 状态：`$EVEROS_HOME/state/`，包括 `sessions/*.json` 会话账本、`localmem.json` 本地记忆同步账本、`hook.log` 日志。
- 新设备、各 agent、云端的装法见 README。

## 6. 维护

- 只追加不删除：更正就是追加一条带准确时间戳的新事实，由 EverOS 自动消解矛盾。
- 只有 owner 明确说某条错了才删：`POST /api/v1/memories/delete {"memory_id":"..."}`。
- 周质检做「补 flush + 查漏 + 纠错」，不推荐删除：
  - 查漏：写过但没产生 episode 的 session，本地记忆有、EverOS 没有的主题；
  - 纠错：agent 做的事被记成 owner 做的，profile 里过期的进度。追加更正时写明依据的 memory_id；
  - 凭据、「低价值 / 一次性」都不算问题。

## 7. 交付自检

对话原文已由 hook 写入的环境里，只需判断本轮有没有要补写的结构化记录：
- 有：写入，并在回复里列出写了什么；
- 没有：回复结尾输出「记忆写入：无」。

## 8. API 速查（账号在 v1；v2 是另一份空库，不要混用）

| 操作 | 接口 | 要点 |
| --- | --- | --- |
| 检索 | `POST /api/v1/memories/search` | `{"filters":{"user_id":…},"query":…,"method":"hybrid","top_k":5,"memory_types":[…]}`；memory_types 可选 `episodic_memory` / `profile` / `raw_message` / `agent_memory` |
| 读取 | `POST /api/v1/memories/get` | memory_type 可选 `episodic_memory` / `profile` / `agent_case` / `agent_skill`；page_size ≤ 100 |
| 写入 | `POST /api/v1/memories` | agent 轨道用 `/api/v1/memories/agent`；必带 `async_mode:false`；消息带 role、sender_id、毫秒 timestamp、content |
| 落盘 | `POST /api/v1/memories/flush` | agent 轨道用 `/api/v1/memories/agent/flush` |

Cloud 按 MemCell 计费，大约 10 条消息算 1 个。
