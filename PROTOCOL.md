# EverOS 记忆协议（唯一依据）

> 2026-10-08 版（v2）。口径：能长期复用的都写、凭据可以入库、默认多写不少写、每条提问都检索。
> 设计理由和踩过的坑见 [docs/methodology.md](docs/methodology.md)；接入新 agent 见 [docs/agent-onboarding.md](docs/agent-onboarding.md)。
> 各 agent 的接线和周质检都以本文件为准。改规则先改这里，再提交、推送。

## 0. 定位

| 载体 | 放什么 |
| --- | --- |
| EverOS（`EVEROS_USER_ID` 指定的空间） | 所有 agent、所有设备共用的记忆主库：存全量，用的时候按需检索 |
| 各 agent 自带的记忆 | 本机速查。Claude Code 的本地记忆由 hook 自动同步进 EverOS |
| 团队知识库、仓库文档 | 权威事实。EverOS 里的内容成熟后按准入流程提炼进去，反向不成立 |

## 1. 接线：谁负责什么

| 能力 | Claude Code / Codex | Kimi Code | 其他 MCP 客户端 |
| --- | --- | --- | --- |
| 会话开始读画像 | SessionStart hook 自动注入（压缩后会再注入一次） | 首条提问时一起注入 | 调 `memory_profile` |
| 每条提问检索 | UserPromptSubmit hook 自动注入 | 同左 | 每条提问先调 `memory_search` |
| 对话原文入库 | Stop hook 每轮自动写入 | 同左 | 不自动；靠 3.2 的结构化记录 |
| 结构化记录、做法、改画像 | MCP：`memory_remember` / `memory_record_practice` / `memory_fix_profile` | 同左 | 同左 |

- 代码都在 `plugin/everos/`，纯 Python 标准库：`core.py` 是读写核心，`hook.py` 是 hook 入口和命令行，`mcp_server.py` 是 MCP 服务。
- session_id：一个 agent 会话对应一个，跨天沿用。Claude Code 用 `cc_<首日>_<标题>_<会话id前8位>`，Codex 用 `cx_…`，Kimi 用 `km_…`。
- botmux 转来的飞书消息按 `<sender>` 标注真实发送者：owner 本人（命中 `EVEROS_OWNER_ALIASES`）记为 owner，其他人记为 `feishu_<邮箱前缀>`。

## 2. 写什么

能长期复用的都写：
- 偏好、纠错、决策及理由；
- 项目事实与进度、任务结果；
- 调研结论、判断推理、背景、业务场景、服务架构；
- 排障路径、工具用法、环境坑；
- 凭据（owner 已授权，但回复里不复述原文）。

不做内容类别过滤，只截断单条超过 2 万字的粘贴。

## 3. 怎么写

### 3.1 自动（hook）

- v2 写入带 `async_mode=false`，不逐轮 flush。
- 消息归属：用户原话的 `sender_id` 是 owner；助手和工具消息统一用 `EVEROS_AGENT_ID`（默认 `<user_id>_agent`），原 agent 名记在 `sender_name`。
- SessionEnd、PreCompact、闲置 2 小时的会话由 hook 补落盘。从 v1 延续下来的会话，换库前会先在 v1 落盘。

### 3.2 结构化记录（agent 在关键节点主动写）

- 时机：任务交付、用户纠错、偏好暴露、决策形成、排障闭环、调研结论、带时间的计划。
- `memory_remember(topic, content, user_said)`：
  - content 写结论、做了什么、证据（文件:行、MR、commit、链接、logid）；
  - user_said 放用户原话，不改写；
  - 同一话题沿用同一个 topic。
- `memory_record_practice(topic, task, steps, conclusion)`：排障或工具用法闭环时写一条做法轨迹。过渡期会同时写 v1：v1 写完立刻生成 case，v2 也会生成但不稳定，观察一周再停。
- `memory_fix_profile(operations)`：用户纠正了画像里的偏好或背景，或者画像里的进度过期了，就直接改。先用 `memory_profile` 拿到 item_id，每条操作都写 reason。

### 3.3 已知坑

完整列表见 [docs/methodology.md](docs/methodology.md#已知坑大多静默失败不报错)。

- 写完要核对：add 返回 `accumulated` 或 `extracted`，并且能用 get 或 search 查到。
- `no_extraction` 有三种情况：缓冲为空、内容已去重、v1 时序问题。前两种正常，第三种 v2 已不存在。
- 兜底命令：`python3 plugin/everos/hook.py --sweep`，补落盘所有待抽取的会话，并同步本地记忆。

## 4. 读

- **会话开始**：注入画像（v2 与 v1 合并）。
- **每条提问**：按提问检索并注入，门控有四道：
  - `EVEROS_MIN_SCORE`（默认 0.3）；
  - 同一会话已注入的不再重复；
  - 每轮 `EVEROS_RECALL_CHARS`（默认 2000 字）；
  - 总预算 `EVEROS_RECALL_TIMEOUT`（默认 18 秒），hook 超时 20 秒。
- **过渡期合并 v1**：v1 没有得分，只在提问有实际内容（去掉标点后不少于 6 个字）时，才带上 v1 的前 3 条。
- **做法检索**：用 `memory_search` 的 `kind=practices`（v2 查 `agent_id`，v1 查 `agent_memory`）。
- 记忆是背景参考；和当前对话冲突时以当前对话为准。

## 5. 配置与部署

环境变量优先，其次 `$EVEROS_HOME/config.env`（默认 `~/.everos/config.env`，0600，永不入库）。

| 变量 | 说明 |
| --- | --- |
| `EVEROS_USER_ID`、`EVEROS_API_KEY` | 必填；云端会话配了网络密钥时可以不填 key |
| `EVEROS_API_VERSION` | `v2`（默认）；`v1` 只为回退保留 |
| `EVEROS_READ_V1` | `1` 表示读取时合并 v1 旧库。官方迁完数据后改成 `0` |
| `EVEROS_AGENT_ID` | v2 助手消息的归属，默认 `<user_id>_agent` |
| `EVEROS_MIN_SCORE` / `EVEROS_RECALL_TIMEOUT` / `EVEROS_RECALL_CHARS` | 检索门槛、预算、字数 |
| `EVEROS_OWNER_ALIASES` | botmux 邮箱前缀别名 |
| `EVEROS_INJECT=0` / `EVEROS_WRITE=0` | 关闭读侧注入 / 写侧入库 |

状态在 `$EVEROS_HOME/state/`：`sessions/*.json` 是会话账本，`recall/*.json` 记录本会话已注入过的条目，`localmem.json` 是本地记忆同步账本，`hook.log` 是日志。

新设备、各 agent、云端的装法见 [AGENTS.md](AGENTS.md) 和 README。

## 6. 维护

- episode 只追加不删；要更正，就追加一条带准确时间戳的新事实。只有 owner 明确要求时才删。
- 画像可以直接改（3.2），但仅限明确的错误或过期内容。
- 周质检做「补落盘 + 查漏 + 纠错」：
  - 查漏：写过但没产生 episode 的 session，本地记忆有、EverOS 没有的主题；
  - 纠错：记错了人的条目，画像里过期的进度。证据明确的用 `memory_fix_profile` 改，证据不足的只写进报告；
  - 不列删除候选，凭据和「低价值」都不算问题。

## 7. 交付自检

对话原文已由 hook 写入的环境里，只需判断本轮有没有要补写的结构化记录：
- 有：写入，并在回复里列出写了什么；
- 没有：回复结尾输出「记忆写入：无」。

## 8. API 速查（v2 为主）

| 操作 | v2 | v1（过渡期只读，做法轨迹双写） |
| --- | --- | --- |
| 写入 | `POST /api/v2/memory/add`：`session_id` + `messages[]`（每条都要有 sender_id、role、毫秒 timestamp、content）、`async_mode` | `/api/v1/memories` 和 `/api/v1/memories/agent`，要带 `user_id` |
| 落盘 | `POST /api/v2/memory/flush`：`session_id` | `/api/v1/memories/flush` |
| 检索 | `POST /api/v2/memory/search`：`query`，`user_id` 和 `agent_id` 二选一，`min_score`、`include_profile`、`filters` | `/api/v1/memories/search`：`filters.user_id`、`memory_types` |
| 读取 | `POST /api/v2/memory/get`：`memory_type` 取 episode / profile / agent_case / agent_skill，可用 `sort_by` | `/api/v1/memories/get`：memory_type 用 `episodic_memory` |
| 改画像 | `POST /api/v2/memory/edit`：`user_id` + `operations[]` | 不支持 |
| 打标签 / 删除 | `/api/v2/memory/tag/{bind,unbind,replace}`、`/api/v2/memory/delete` | — |

Cloud 按 MemCell 计费，大约 10 条消息算 1 个。v1 即将退役，届时官方会把 v1 数据迁到 v2。
