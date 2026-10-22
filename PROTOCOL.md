# EverOS 记忆协议（唯一依据）

> 2026-10-08 版（v2）。口径：能长期复用的都写、凭据可以入库、默认多写不少写、每条提问都检索。
> 设计理由和踩过的坑见 [docs/methodology.md](docs/methodology.md)；EverOS 服务本身的诀窍、局限与坑点见 [docs/everos-notes.md](docs/everos-notes.md)；接入新 agent 见 [docs/agent-onboarding.md](docs/agent-onboarding.md)。
> 各 agent 的接线和周质检都以本文件为准。改规则先改这里，再提交、推送。

## 0. 定位

| 载体 | 放什么 |
| --- | --- |
| EverOS（`EVEROS_USER_ID` 指定的空间） | 所有 agent、所有设备共用的记忆主库：存全量，用的时候按需检索 |
| 各 agent 自带的记忆 | 本机速查。Claude Code 的本地记忆由 hook 自动同步进 EverOS |
| 团队知识库、仓库文档 | 权威事实。EverOS 里的内容成熟后按准入流程提炼进去，反向不成立 |

## 1. 接线：谁负责什么

| 能力 | Claude Code / Codex / TraeX | Kimi Code / Kimi Work 桌面端 | MiMoCode / MiMo Desktop | 其他 MCP 客户端 |
| --- | --- | --- | --- | --- |
| 会话开始读画像 | SessionStart hook 自动注入（压缩后会再注入一次） | 首条提问时一起注入 | Plugin `system.transform` / 首条提问注入 | 调 `memory_profile` |
| 每条提问检索 | UserPromptSubmit hook 自动注入 | 同左 | Plugin `chat.message` 注入 synthetic part | 每条提问先调 `memory_search` |
| 对话原文入库 | Stop hook 每轮自动写入 | 同左 | Plugin `session.post` → `--mimo-write` | 不自动；靠 3.2 的结构化记录 |
| 结构化记录、做法、改画像 | MCP：`memory_remember` / `memory_record_practice` / `memory_fix_profile` | 同左 | 同左 | 同左 |

- 代码都在 `plugin/everos/`，纯 Python 标准库：`core.py` 是读写核心，`hook.py` 是 hook 入口和命令行，`mcp_server.py` 是 MCP 服务。MiMoCode 的 L3/L4 入口是 `plugin/mimo/everos.mjs`，经 `hook.py --mimo-recall/--mimo-write` 复用同一套读写。
- session_id：一个 agent 会话对应一个，跨天沿用。Claude Code 用 `cc_<首日>_<标题>_<会话id前8位>`，Codex 用 `cx_…`，Kimi Code 用 `km_…`，Kimi Work 桌面端用 `kw_…`，MiMoCode 用 `mc_…`。TraeX 用 `tx_<首日>_<标题>_<完整会话id>`，避免 UUIDv7 时间前缀相同导致跨会话混写。
- TraeX ≥ 0.209.1：`SessionStart` / `UserPromptSubmit` 注入，`Stop` 增量采集，`SessionEnd` / `PreCompact` 补写与落盘；`PostCompact` 只标记下轮补读画像。当前注入已存在时，规则不再重复调用 MCP。hook 必须启用且被信任。
- TraeX 读取原始 rollout 的 `history_mutation` v1 append（兼容旧 `response_item`），只保存用户与助手的文本；用 `content_item_kinds` 区分用户输入与环境/规则/注入，不采集工具镜像或 reasoning。替换/压缩快照不作为新对话重新入库。`transcript_path` 为空时按精确 session_id 只读查索引或匹配 rollout 文件，找不到会记录错误且不推进采集进度。
- Kimi Work 桌面端：hook 挂在 App 内嵌 runtime 的 `config.toml`（daimon 托管，App 升级会重写，丢了重跑 `install.sh`）；hook 命令带 `EVEROS_HOST=kimi_desktop` 显式指定宿主；`ctitle-*` 标题侧会话不采集。
- MiMoCode：无 hooks.json，L3/L4 走 Plugin API；轨迹在 `session.post` 内存直传，每轮写完即 flush；配置写 `~/.config/mimocode/mimocode.jsonc`，新会话生效。
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
- add 只有返回已识别的成功状态才推进会话读取偏移；HTTP 成功但业务失败、未知状态或异常时保留偏移，下一次 hook 可重试。flush 只有 `extracted` / `no_extraction` 才清除 pending。
- 消息归属：用户原话的 `sender_id` 是 owner；助手和工具消息统一用 `EVEROS_AGENT_ID`（默认 `<user_id>_agent`），原 agent 名记在 `sender_name`。
- SessionEnd、PreCompact、闲置 2 小时的会话由 hook 补落盘。从 v1 延续下来的会话，换库前会先在 v1 落盘。

### 3.2 结构化记录（agent 在关键节点主动写）

- 时机：任务交付、用户纠错、偏好暴露、决策形成、排障闭环、调研结论、带时间的计划。
- `memory_remember(topic, content, user_said)`：
  - content 写结论、做了什么、证据（文件:行、MR、commit、链接、logid）；
  - user_said 放用户原话，不改写；
  - 同一话题沿用同一个 topic。
- `memory_record_practice(topic, task, steps, conclusion)`：排障或工具用法闭环时写一条做法轨迹。
  - 至少 3 个真实步骤，写入前校验。只有一两步的结论改用 `memory_remember`，不补写没发生的步骤。
  - 走错的路、失败的尝试单独记一步，标 `outcome=failed`；出乎意料的结果标 `outcome=unexpected`。`result` 第一句写这一步的发现。
  - 用户中途纠正或改向时，原话填进那一步的 `user_correction`，作为独立的用户消息写入，不改写。
  - v2 只把有试错、意外、非显然诊断或用户纠正的轨迹提炼成做法 case，顺利执行的线性流程通常不会有 case，内容仍以普通记忆可检索。门槛和实测见 [EverOS 诀窍与坑点](docs/everos-notes.md#3-做法-case-怎么生成v2)。
  - 写完在 `EVEROS_CASE_WAIT` 秒内回读 v2 做法 case，结果写在返回里；没生成不算失败。
  - 过渡期同时写 v1，v1 几乎每条都出 case。停双写的条件见 [演进路线](docs/methodology.md#演进路线)。
- 实践双写任一版本失败时，工具返回错误并保留各版本结果，不做 case 回读；成功的版本可能已经写入，重试仍沿用同一 topic。写入与抽取成功不代表搜索索引已经可见，需要回读确认。
- `memory_fix_profile(operations)`：用户纠正了画像里的偏好或背景，或者画像里的进度过期了，就直接改。先用 `memory_profile` 拿到 item_id，每条操作都写 reason。

### 3.3 已知坑

接线相关的坑见 [docs/methodology.md](docs/methodology.md#已知坑大多静默失败不报错)，EverOS 服务本身的坑见 [docs/everos-notes.md](docs/everos-notes.md)。

- 写完要核对：add 返回 `accumulated` 或 `extracted`，并且能用 get 或 search 查到。
- `no_extraction` 有三种情况：缓冲为空、内容已去重、v1 时序问题。前两种正常，第三种 v2 已不存在。
- 日志和工具返回里的 `A/B` 是同一版本的「add 状态/flush 状态」，不是「v1/v2」；做法双写写成 `v2 A/B; v1 A/B`。
- `extracted` 不代表生成了做法 case。做法 case 是否生成，看 `memory_record_practice` 返回里的回读结论，或 hook.log 的 `practice case … found/missing`。
- MCP 参数校验失败（`-32602`）的请求没有发出，修正参数后重写，不能当成已入库。
- 兜底命令：`python3 plugin/everos/hook.py --sweep`，补落盘所有待抽取的会话，并同步本地记忆。
- 本地记忆的三次非成功重试限制按文件版本计算；正文更新后重新允许尝试。网络异常不消耗内容重试次数，`no_extraction` 可视为已去重完成。

## 4. 读

- **会话开始**：注入画像（v2 与 v1 合并），SessionStart 读侧最多等待 6 秒，低于宿主 10 秒上限。
- **每条提问**：按提问检索并注入，门控有四道：
  - hybrid 回退使用 `EVEROS_MIN_SCORE`（默认 0.3），agentic 不套用该门槛；
  - 同一问题的同一内容版本已展示时去重；新问题、正文更新或压缩恢复允许重读；
  - 每轮 `EVEROS_RECALL_CHARS`（默认 2000 字）；
  - 总预算 `EVEROS_AGENTIC_TIMEOUT`（默认 180 秒，含回退），hook 超时默认 190 秒。
- Kimi 首轮画像与召回共用上述总预算，画像最多占用 6 秒。超时后采用已返回的结果，不等待其他请求；去重账本只记录实际注入的条目。
- **过渡期合并 v1**：v1 没有得分，只在提问有实际内容（去掉标点后不少于 6 个字）时，才带上 v1 的前 3 条。
- **做法检索**：用 `memory_search` 的 `kind=practices`（v2 查 `agent_id`，v1 查 `agent_memory`）。
- **召回数量**：`memory_search.top_k` 与官方范围一致：`-1` 或 `1–100`，默认仍为 `5`。`-1` 交给服务端决定；用户记忆、做法和 v1 兼容查询都传同一个值，不再截成 20 或把做法固定为 3。非法值直接报错。hook 仍传 `5`，旧库前 3 条门控、得分门槛和输出字数预算照常生效。
- **检索方法**：每条用户消息触发的首次记忆检索必须使用 `agentic`，覆盖同一会话的首轮和后续轮。自动 hook 显式传 `method=agentic`；没有 hook 或缺少本轮注入时主动调用 `memory_search(method="agentic")`。MCP 省略 method 也默认 agentic；后续补查可显式选 `hybrid`。不得先跑 hybrid 再决定是否深查。本轮同一问题已有充分证据时复用，新问题或缺口仍应补查。独立只读 CLI 保留默认 hybrid，用户可通过 `--method agentic` 选择深查。不默认开启 `enable_llm_rerank`。
- **深检索预算与回退**：自动 hook / MCP / CLI agentic 的默认共同总预算为 180 秒，可通过 `EVEROS_AGENTIC_TIMEOUT`（环境变量优先于 config.env）修改，也可用 MCP `timeout` / CLI `--timeout` 按次覆盖。值须为正有限秒数，非法配置明确报错。默认先给 agentic 约 168 秒、为 hybrid 回退保留至多 12 秒，不能在总预算之外再等待。v2 用户和做法轨道分别回退，成功轨道保持不变；失败、超时或空结果会按轨道尝试 hybrid，输出明确提示。全部轨道技术失败会报错，成功空结果正常返回无记忆。v1 兼容请求始终用 hybrid；v1-only 下显式选择 agentic 会提示降级，不向 v1 发送 agentic。自动 hook 复用同一检索核心和按轨道回退，并将降级提示注入上下文。
- **方法专属门槛**：agentic 的用户请求不发送 `min_score`，其做法结果不套用 hybrid 的 `0.55` 门槛；采用服务端排序并保留输出字数预算。回退到 hybrid 的用户请求恢复 `EVEROS_MIN_SCORE`，做法结果恢复 `0.55`；v1 沿用原门控。不同方法得分不可直接比较，召回内容仍需按当前问题判断相关性。
- **手动检索**：`everos search "问题"` 与 MCP 共用检索核心和配置，支持同样的 `kind`、`top-k`、`method`，不写入记忆、不触发 hook、不更新会话去重账本。CLI 启用严格读错误处理：所有轨道失败或超时返回非零；成功空结果返回零；部分轨道失败提示结果不完整。hook 与 MCP 保留原有默认错误策略。文本仍采用单条 260 字、整体约 4000 字的摘要格式，问题超过 500 字时 CLI 拒绝而非静默截断。安装和示例见 README。
- **质量与时间的取舍**：owner 于 2026-10-09 确认，若检索效果有较显著提升，稍长的检索时间可以接受。方法选择以目标记忆命中、无关结果和最终回答质量为主；2026-10-21 owner 明确要求每条消息首次检索使用 agentic，取代此前 hybrid 默认；预算与门槛使用方法专属规则，不把历史对比当作当前质量保证。
- 记忆是背景参考；和当前对话冲突时以当前对话为准。

### 4.1 任务内检索与正文展开

每条消息首次agentic是起点。方案/行动前、新线索/错误/失败、追问历史决定前、上下文压缩恢复后，Agent 检查历史证据缺口，必要时主动补查。已有同一问题的充分证据可复用。短规则、工具语义和真实场景示例同时维护；不得用工具调用次数衡量质量。

- `memory_search` 可带 `context`（最多500字）、`session_id`，结合当前问题与任务线索查询；不改变已有query/kind/method/top_k调用。
- 结果带本机 `em_` 标识、来源、日期和截断状态。`memory_open(memory_id, offset, limit)`读取服务端记忆快照的完整内容，单页默认4000字、最多12000字，返回next_offset；它不声称返回原始聊天，也不代表实时事实。
- 快照在 `$EVEROS_HOME/state/reads/`，按后端、owner、agent、来源、类型、原ID与投影后的内容生成标识，文件权限0600；不缓存embedding或传输凭据。跨身份拒绝读取；迁移到其他设备或快照缺失时重新检索，不猜路径、不全库扫描。
- `memory_context` 更新本机会话goal/stage/clues。检索覆盖按问题与内容版本记录，最多保留24个问题；记录仅证明候选曾展示，不证明Agent已读懂或证据充分。目标改变或recover/压缩清除覆盖；首轮旧账本兼容迁移后按新规则处理。
- Claude Code / Codex 的 PreToolUse 和 PostToolUse 使用短小本地提醒：方案/写入/提交动作前、失败输出后检查缺口；每种相同动作/异常只提醒一次，不递归提醒memory工具，不执行网络请求，不批准或阻止动作，失败放行。
- TraeX 0.210.1起接入PreToolUse/PostToolUse（旧版保留六个生命周期事件）；MiMo 0.1.15 Plugin API接入tool.execute.before/after和experimental.session.compacting。MiMo的before提示进入下次模型上下文，不阻止或重写当前动作；after提示追加工具输出。Kimi及仅MCP宿主通过共享规则和工具补查，不声明未支持的自动工具事件。
- 关闭注入时不运行任务提醒；关闭写入时仍可本机检索、展开和维护临时上下文，但不写EverOS。历史权限、实时状态、记忆冲突继续按当前指令与Owning Source核实。

架构、各宿主能力与隔离行为评测见 [检索工作流](docs/retrieval-workflow.md)。

## 5. 配置与部署

MCP子进程可能过滤父进程环境，安装器在各客户端支持的env/environment字段或CLI --env中显式绑定EVEROS_HOME，避免hook与MCP读取不同状态目录；只写目录，不复制凭据。Claude插件按其原生继承环境运行。环境变量优先，其次 `$EVEROS_HOME/config.env`（默认 `~/.everos/config.env`，0600，永不入库）。

| 变量 | 说明 |
| --- | --- |
| `EVEROS_USER_ID`、`EVEROS_API_KEY` | 必填；云端会话配了网络密钥时可以不填 key |
| `EVEROS_API_VERSION` | `v2`（默认）；`v1` 只为回退保留 |
| `EVEROS_READ_V1` | `1` 表示读取时合并 v1 旧库。官方迁完数据后改成 `0` |
| `EVEROS_AGENT_ID` | v2 助手消息的归属，默认 `<user_id>_agent` |
| `EVEROS_MIN_SCORE` / `EVEROS_RECALL_TIMEOUT` / `EVEROS_RECALL_CHARS` | hybrid 检索门槛、旧版画像预算、注入字数；EVEROS_RECALL_TIMEOUT 不再限制提问检索 |
| `EVEROS_AGENTIC_TIMEOUT` | 自动/手动 agentic 检索总预算（秒，含回退），默认 180；MCP `timeout` / CLI `--timeout` 可按次覆盖 |
| `EVEROS_CASE_WAIT` | 写做法后回读 v2 做法 case 的最长等待（秒），从 v2 写完开始计，默认 30，上限 150，`0` 关闭回读；非法值按默认处理 |
| `EVEROS_OWNER_ALIASES` | botmux 邮箱前缀别名 |
| `EVEROS_INJECT=0` / `EVEROS_WRITE=0` | 关闭读侧自动注入 / 所有写侧入库（包括 MCP、flush、画像编辑、本地记忆同步） |
| `EVEROS_HOST` | hook 命令里显式指定宿主（如 `kimi_desktop`），桌面端 payload 没有 `client_type`，靠它区分 |
| `EVEROS_KIMI_DESKTOP_HOME` | Kimi Work 桌面端内嵌 home，默认 `~/Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home` |

状态在 `$EVEROS_HOME/state/`：`sessions/*.json` 是会话账本，`recall/*.json` 记录本会话已注入过的条目，`localmem.json` 是本地记忆同步账本，`hook.log` 是日志。

MCP 客户端外层超时需大于实际检索预算。安装器为 Codex 的 `tool_timeout_sec` 和 MiMo 的毫秒 `timeout` 设置至少配置预算 + 10 秒，默认 190 秒，并保留更大的现值。调大配置后重跑 `install.sh`；Kimi / TraeX hook 和 MiMo 插件外层同样随预算更新。Claude Code / Codex 插件及云端静态 hook 定义默认 190 秒，预算超过 180 秒时须同步提高其 UserPromptSubmit timeout 并重新审阅、信任；按次增加预算时也需保证外层允许等待。其他宿主按自身协议配置，不猜测字段。`memory_record_practice` 的耗时是写入（通常 10 到 20 秒）加回读等待（与 v1 双写重叠，最多 `EVEROS_CASE_WAIT` 秒），默认值在 190 秒内；宿主外层超时更短时调小 `EVEROS_CASE_WAIT`。

Codex 升级前必须保存已有 EverOS 插件版本缓存，CLI 操作后恢复被移除的旧目录（操作失败时也恢复）。运行中的聊天仍可能持有旧版 hook 的绝对路径；不能仅安装新版本就删除旧入口，也不能把旧版本路径指向不同内容。新聊天加载新版本，旧缓存不自动清理。

TraeX 的共享配置/规则位于 `$TRAE_HOME`（默认 `~/.trae`），运行目录由 `$TRAECLI_HOME` 覆盖（默认 `$TRAE_HOME/cli`），hook 写在运行目录。安装器备份并只替换 EverOS handler，保留其他 hook 及信任记录，不自动授予信任。可用 `install.sh --only trae` 单独更新。支持的入口优先级为 traecli、traex、coco；旧 Coco 保留 MCP + 规则，不宣称具备 L3/L4。

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
| 读取 | `POST /api/v2/memory/get`：`memory_type` 取 episode / profile / agent_case / agent_skill，可用 `sort_by`；`filters` 支持 `session_id`、`timestamp` 等，不能放 `user_id`/`agent_id`；返回体按 `memory_type` 取对应列表键 | `/api/v1/memories/get`：memory_type 用 `episodic_memory` / `agent_case` / `agent_skill` / `profile`（`agent_memory` 只用于 search） |
| 改画像 | `POST /api/v2/memory/edit`：`user_id` + `operations[]` | 不支持 |
| 打标签 / 删除 | `/api/v2/memory/tag/{bind,unbind,replace}`、`/api/v2/memory/delete`（按 `user_id` / `agent_id` / `session_id` 软删除）、`/api/v2/memory/delete_by_ids` | — |

Cloud 按 MemCell 计费，大约 10 条消息算 1 个。v1 即将退役，届时官方会把 v1 数据迁到 v2。
