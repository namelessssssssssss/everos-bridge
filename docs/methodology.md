# EverOS 记忆读写方法论

PROTOCOL.md 讲「怎么做」，本文讲「为什么这样做」：原则、关键取舍，以及踩过的坑。改规则前先读这里，避免把踩过的坑再踩一遍。

## 目标

用户在任何设备、任何 agent、任何工作区里，都能自动拿到同一份长期记忆；平时的工作也会自动沉淀进去。新工作区只要让 agent 读这个仓库，就能重建整套流程。

## 原则

### 1. 一个主库

EverOS 是所有 agent、所有设备共用的记忆主库。

- 各 agent 自带的记忆只是本机缓存。比如 Claude Code 的 CLAUDE.md 和 `~/.claude/projects/*/memory/`、Codex 的 `~/.codex/memories`，都不跨设备，也不进云端会话。
- Claude Code 的本地记忆由 hook 自动同步进 EverOS。
- 团队知识库、仓库文档才是权威事实。EverOS 里的内容成熟后，按准入流程提炼进去。

### 2. 默认全写，靠机制不靠自觉

- **原文由 hook 自动入库**：对话原文每轮由 hook 写入，不依赖 agent 记得去写。
- **agent 只补结构化记录**：在关键节点写结论、决策及理由、纠错、做法轨迹。
- **理由**：2026-10 排查发现，旧做法由 agent 自己判断要不要写、再写摘要，结果：
  - 六成写入因为接口时序问题没被抽取；
  - agent 做的事常被记成用户做的；
  - 各宿主的规则口径不一致。

### 3. 写法决定质量

- 用户原话放 user 消息，不改写；结论、动作、证据放 assistant 消息。两边分开，抽取时才不会把 agent 做的事记成用户做的。
- 排障按「现象 → 根因 → 解法 → 验证」写。
- 调研写「结论 + 来源 + 截至何时有效」。
- 决策写「选了什么 + 为什么不选另一个」。
- 做法写成轨迹：需求 → 每步动作与结果 → 结论，交给 EverOS 提炼成可复用的经验。

### 4. 每条提问都检索，但要有门控

- 会话开始注入画像。
- 每条提问前按提问检索并注入，这也是官方插件的默认做法。门控有四道：
  - 每条消息首次使用 agentic；hybrid 回退才恢复 v2 `min_score`（默认 0.3）；
  - 同一问题与内容版本去重，问题改变、内容更新或压缩后允许重读；
  - 每轮最多约 2000 字；
  - 总预算 180 秒，hook 超时默认 190 秒（owner 2026-10-21 确认每条消息首次走 agentic）。
- 过渡期合并 v1 旧库时，因为 v1 没有得分，只在提问有实际内容时才带上它的前 3 条。
- 压缩后会重新触发会话开始注入，画像会再给一次。
- 超时以整轮读侧的 deadline 为准，不能在线程池退出时继续等待；Kimi 首轮画像与检索共享预算。召回去重只记实际展示的条目，不能从文本换行数反推条目数。

### 5. 维护：episode 只追加，画像可以改

- episode 不删除；要更正，就追加一条带准确时间戳的新事实。
- 画像在 v2 里可以直接改（`memory_fix_profile`），但只用于明确的错误或过期内容，并写明依据。
- 周质检做三件事：补落盘、查漏、纠错。不列删除候选，也不把凭据当问题。

### 6. 安全与边界

- key 只放在本机的 `~/.everos/config.env`（0600），或者云端的网络密钥里，永不入库。
- 内容不做类别过滤（owner 2026-10-08 确认），只截断单条超过 2 万字的粘贴。
- 所有 hook 失败都放行，记忆读写永远不值得打断一次对话。
- 放行会话不等于把入库失败当成功：业务错误保留会话偏移和 pending，MCP 返回错误。实践双写任一版本失败时保留分版本结果，允许重试；`EVEROS_WRITE=0` 统一禁止写入，包括 MCP 和画像编辑。

## 关键取舍（决策记录）

| 日期 | 决策 | 理由 |
| --- | --- | --- |
| 2026-10-08 | 自建 everos-bridge，不用官方 EverMe 或 Plugins | EverMe 连的是另一个后端，官方 Plugins 走 v2 空库，都读不到用户现有的 v1 数据；官方的 Claude Code 插件也只覆盖单个宿主 |
| 2026-10-08 | 一个插件目录同时给 Claude Code 和 Codex 用，Kimi 走 config.toml，其他 agent 走 MCP | 三家的 hook 机制相近，MCP 是最通用的兜底 |
| 2026-10-08 | 现在就切 v2：新写入只进 v2，读取时合并 v1 | v1 即将退役，v2 不丢写入、不丢旧时间戳、能设得分门槛、能改画像；旧数据由官方迁移，迁完就关掉 v1 读取 |
| 2026-10-08 | 每条提问都检索，超时 20 秒 | owner 选择：完整性优先于延迟 |
| 2026-10-08 | 做法轨迹同时写 v1 | v2 会生成 case，但不稳定：真实空间里有一条轨迹出了 case（质量 0.9），探针空间 10 分钟仍为 0；v1 写完立刻就有。连续观察一周 v2 稳定生成后再停 |
| 2026-10-08 | Kimi Work 桌面端接 App 内嵌 runtime（`runtime/kimi-code/config.toml` + `home/mcp.json`），宿主用 `EVEROS_HOST=kimi_desktop` 显式指定 | 桌面端没有独立 CLI，payload 没有 `client_type`；内嵌引擎（kimi-code 0.15.7）与 CLI 同源，hook / MCP / wire.jsonl 机制完全一致，解析器直接复用 |
| 2026-10-09 | `top_k` 接受 `-1` 或 `1–100`，所有查询轨道沿用调用值，默认仍为 5 | 原 20 条截断和做法固定 3 条没有记录依据；官方已有范围校验，注入量另由得分和字数预算控制。`-1` 必须原样传递，不能变成 1 或在 Python 切片中丢掉末条 |
| 2026-10-09 | 保留 hybrid 默认，不默认开启 LLM rerank 或 agentic | [4 问题、10 请求的小样本](retrieval-study-2026-10-09.md)：rerank 未改变两个做法问题的前 5 条排序；agentic 有根因排名收益，也有超过 18 秒预算和得分门槛不兼容的样本。尚不能证明默认切换更好 |
| 2026-10-09 | 效果优先，若有显著质量收益可接受稍长检索时间 | owner 明确补充这一取舍；18 秒只作为当前运行值，不能一票否决更好的方法。先扩大固定目标问题的对比，再根据实际命中、噪声和回答质量决定是否放宽预算 |
| 2026-10-09 | MCP 开放按次 method=agentic，默认保留 hybrid | 9题42次对比中，隐含历史描述出现两轮一致补漏；给深查45秒共同预算并按轨道回退。agentic低分正确结果不能套hybrid门槛，回退hybrid后恢复门槛；升级至0.3.3，旧会话仍保留原缓存 |
| 2026-10-10 | TraeX 接入 L3/L4，沿用 stdio MCP + 用户级 hook | 0.209.1 已支持完整生命周期；独立适配 history_mutation 和运行目录，正常信任后实测注入/采集，规则只在缺注入时补读 |
| 2026-10-12 | 提供只读的 `everos search` CLI，复用检索核心，启用严格读错误处理 | 手动检索无需打开 agent；通过复用 v1/v2 合并和 agentic 回退避免两套检索逻辑。成功空结果与断网区分，部分轨道失败明确提示；`install.sh --cli-only` 仅安装入口，不重接宿主 |
| 2026-10-12 | agentic 默认总预算改为180秒，并开放配置和按次覆盖 | owner反馈45秒经常超时；用EVEROS_AGENTIC_TIMEOUT及MCP timeout / CLI --timeout调整，回退仍共享deadline。Codex/MiMo外层至少配置预算+10秒，避免宿主先截断；hook默认预算保持不变 |
| 2026-10-16 | 做法写入前校验至少 3 步；步骤可标 `outcome`、可带 `user_correction`；写完回读 v2 做法 case 并在返回里说明 | 分库核对发现约三分之一做法没有 v2 case，写入返回却一样是成功。读开源实现确认 v2 有提炼筛选（少于 3 轮、线性流程、无纠正都会跳过），v1 没有。隔离空间实测：合格轨迹 25 次里 24 次在 4 到 12 秒内出 case，顺利执行的线性流程不出，默认回读 30 秒。新旧消息映射在样本里生成率相同；保留新映射是为了让失败和用户纠正以独立消息出现。细节见 [EverOS 诀窍与坑点](everos-notes.md#3-做法-case-怎么生成v2) |
| 2026-10-16 | 停 v1 双写的标准改为：写得合格的做法在 v2 稳定出 case | v2 的筛选是设计如此，线性流程永远不会有 case，覆盖率不可能追平 v1；这些内容仍以 v2 普通记忆可检索 |

2026-10-21：owner 确认每条用户消息首次记忆检索总是使用 agentic，覆盖 hook 和无 hook 的 MCP 规则。以上保留 hybrid 默认的旧决策已被本次取代；按轨道 hybrid 回退、v1 兼容、会话去重和字数上限继续生效。自动检索改用 EVEROS_AGENTIC_TIMEOUT，避免旧 EVEROS_RECALL_TIMEOUT=18 提前截断。

## 已知坑（大多静默失败，不报错）

| 坑 | 现象 | 应对 |
| --- | --- | --- |
| v1 add 默认异步 | 紧跟着的 flush 返回 `no_extraction`，单次写入的 session 永远不抽取 | v1 写入带 `async_mode=false`；v2 没有这个问题 |
| v1 丢弃旧时间戳 | 约 2 个月前的时间戳，接口返回 accumulated，但永远不抽取 | 补录旧事用当前时间，原始日期写进正文；v2 实测 62 天前可以入库 |
| v1 检索得分全为 0 | 没法按相关度过滤，像「继续」这样的提问也会召回不相关的内容 | 用 v2 的 `min_score`；过渡期 v1 只在提问有实际内容时才用 |
| EverOS 内容去重 | 内容已经存在时返回 `no_extraction` | 正常现象，不算失败 |
| Codex 插件是拷贝安装 | 改了代码，Codex 不生效 | 重跑 `install.sh` |
| Codex 更新移除旧版缓存 | 运行中的聊天仍调用旧版绝对路径，Stop 报 `hook.py: No such file or directory` | 安装器在 CLI 操作前保存旧缓存，操作后恢复被移除的版本，失败时也恢复；保留原版本内容，供旧聊天收尾，新聊天加载新版。2026-10-09 从 0.3.1 升至 0.3.2 时实测发现 |
| Codex 插件 hook 未信任 | 插件 enabled、MCP 可连接，但没有自动注入和采集 | 在新会话审阅并信任当前定义；更新后核验实际注入和回读 |
| HTTP 200 中包含业务错误 | add 看似成功却没有落库，偏移推进后无法重试 | 校验响应和写入状态；异常时不推进 offset，flush 未完成保留 pending |
| 部分实践双写失败 | 一边成功、一边失败仍可能被误读成全成功 | MCP 报错并保留两版本结果；已成功版本可能写入，沿用 topic 重试 |
| 召回条目含多行 | 未展示的后一条记忆也被标记 seen | 格式化时返回实际展示的 ID；字数截断不影响未展示项后续召回 |
| 本地记忆重试次数耗尽 | 文件更新后仍被旧失败次数永久挡住 | 次数按文件 mtime 版本计算；内容更新重置，网络异常不消耗内容次数 |
| 新实践 case 查询为空 | 写入/抽取已经成功，但首次搜索仍没有结果 | 区分抽取与索引可见性，延后回读；v2 case 稳定性仍按周观察 |
| v2 做法 case 有提炼筛选 | 写入返回 `extracted`，却没有做法 case；2026-10 约三分之一的做法如此 | 写前校验至少 3 步；按 [EverOS 诀窍与坑点](everos-notes.md#4-写出能被提炼的做法) 写出试错和纠正；看返回里的回读结论 |
| 日志里的 `A/B` 被读成 v1/v2 | 看到 `extracted/no_extraction` 误以为 v2 没写 | 它是同一版本的 add/flush；双写写成 `v2 A/B; v1 A/B` |
| MCP 参数校验失败被当成已写入 | 缺字段的调用当场被拒，`-32602`，请求没发出 | 修正参数重写；统计写入时不计这类调用 |
| 在宿主加载的仓库目录里开发 | 本机各宿主直接加载 `~/.everos-bridge`；在这里切分支或改代码，会立刻影响每次新起的 hook 和 MCP | 开发用 `git worktree`（如 `~/.everos-bridge-<名称>`），宿主加载的目录保持 main，合并后再 pull 并重跑 `install.sh` |
| 安装器旧路径与配置残缺 | 重跑仍引用旧 clone，或缺少部分 hook | 精确迁移本插件配置，保留其他插件字段；修改前做唯一命名备份，失败返回非零 |
| Codex 读插件的 `.mcp.json` | 参数里的 `${CLAUDE_PLUGIN_ROOT}` 不确定会不会展开 | MCP 用绝对路径写进 Codex 的 config.toml；Claude Code 的 MCP 声明放在 `claude-mcp.json` |
| Kimi 的 SessionStart 只通知、不注入 | 画像注入不进上下文 | 把画像放到首条提问时一起注入；Kimi 用纯文本 stdout |
| Kimi 的 hook 输入不带会话记录路径 | 无法直接读取会话记录 | 按 session_id 去找 `~/.kimi-code/sessions/*/session_<id>/agents/main/wire.jsonl` |
| Claude Code 的 SessionEnd 预算约 1.5 秒 | 插件里的 timeout 撑不大，重活会被打断 | 写入一律转到后台子进程 |
| botmux 转来的飞书消息 | 同事的话会被记到 owner 名下 | 按 `<sender>` 标注真实发送者，`EVEROS_OWNER_ALIASES` 认 owner |
| MCP 服务是常驻进程 | 改了插件代码后，已打开会话里的 MCP 还是旧代码（hook 每次新起进程，立即生效） | 新开会话，或在 Claude Code 里执行 `/reload-plugins` |
| Claude Desktop 运行时覆盖配置 | App 开着时，外部写进 `claude_desktop_config.json` 的 MCP 会被它自己保存偏好时冲掉 | 退出 App 后再写，或在 App 设置里加；安装脚本检测到 App 在运行就不写 |
| 非 UTF-8 locale 下的 bash | `$VAR（` 这种写法，会把全角字符的字节当成变量名的一部分，`set -u` 时报 unbound variable | 紧跟非 ASCII 字符的变量一律写成 `${VAR}`（2026-10-08 新 agent 自举测试中发现） |
| Kimi Work 桌面端的 config.toml 被 App 重写 | App 升级 / 重 provisioning 后，手加的 `[[hooks]]` 没了 | 重跑 `install.sh`（幂等）；hook 装在 `runtime/kimi-code/config.toml`，不是 `~/.kimi-code` |
| 桌面端 hook 在会话启动时加载 | 改了 hook 配置，当前会话不生效 | 新开会话验证；别在旧会话里反复试 |
| 桌面端的 ctitle-* 侧会话 | 标题生成也会触发 UserPromptSubmit / Stop，把标题生成提示词当对话入库 | 按 session_id 前缀 `ctitle-` 整体跳过（不检索、不采集） |
| 桌面端用户消息带 `<meta>` 前导 | 入库的用户原话开头带 `<meta awareness="low" .../>` | `messages_kimi` 解析时剥掉前导 `<meta .../>` |
| 桌面端 hook payload 没有 client_type | 没法像 Kimi Code CLI 那样靠 `client_type` 识别宿主 | hook 命令带 `EVEROS_HOST=kimi_desktop`（引擎经 shell 执行，环境变量前缀有效）；stdin 字段是 snake_case（引擎自动从驼峰转换） |
| TraeX 新 rollout 不再写 response_item | 旧 Codex 解析器返回空消息，安装表面成功但无采集 | 读取 history_mutation v1 append 中的原始 items；不重放 display_completions、event_msg 和 compact 快照 |
| TraeX 共享目录和运行目录不同 | 把 hook 写到 ~/.trae/hooks.json 可能只在该目录作为项目时生效 | 用户级 hook 写 `$TRAECLI_HOME/hooks.json`，默认 ~/.trae/cli/hooks.json；MCP/规则仍在 `$TRAE_HOME` |
| TraeX config API 的 keyPath 按点分割 | hook 路径含点时，带引号的 keyPath 仍可能拆错 | 对 `hooks.state` 写完整键对象，并用 hooks/list 回读信任状态；安装器本身不自动信任 |
| TraeX 主目录重复发现同一 hook | 同一路径同时显示为 user / project，事件触发两次 | 读侧按 session 加锁、按 turn + prompt 去重，SessionStart 同来源两秒内去重；写侧沿用文件锁与偏移 |
| TraeX UUIDv7 前八位相同 | 一分钟内创建的两个同名工作区会话可能混写 | TraeX 的 EverOS session 保留完整 UUID，其他宿主命名保持兼容 |

## 演进路线

1. 官方迁完 v1 数据后，设 `EVEROS_READ_V1=0`，删掉 v1 代码路径。
2. 写得合格的做法（至少 3 步，写出了试错、意外或用户纠正）在 v2 连续一周稳定生成 case 后，停掉 v1 双写。统计用 hook.log 的 `practice case … found/missing`；线性流程没有 case 属于设计，不计入。
3. 子代理注入（SubagentStart）、工具触发召回（PostToolBatch），在 v2 得分门槛稳定后再开。
4. 把 PROTOCOL 和知识卡这类文档导入 v2 知识库，跟对话记忆一起检索。

2026-10-22：将记忆检索纳入任务过程：短规则明确补查时机；搜索结果可按ID分段展开；本机上下文/覆盖按问题与版本记录；已支持工具事件的宿主使用无网络提醒。以任务正确性和必要补查行为评测，不把检索次数当质量。详见 retrieval-workflow.md。
