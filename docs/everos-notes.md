# EverOS 使用诀窍、局限与坑点

本文记录 EverOS 服务本身的行为：写入状态怎么读、v1 和 v2 的差异、做法 case 怎么生成、读取和删除接口的限制，以及实测得到的诀窍。接线和各宿主的坑见 [methodology.md](methodology.md#已知坑大多静默失败不报错)，读写规则见 [PROTOCOL.md](../PROTOCOL.md)。

出处分三类，每节都会注明：
- **官方文档**：docs.evermind.ai。
- **开源实现**：EverMind-AI/EverOS 及其算法库 EverAlgo。Cloud 不一定与开源版本完全一致。
- **实测**：本仓库在真实空间或隔离测试空间里的测量，带日期。

## 1. 写入状态怎么读

出处：官方文档、实测。

| 调用 | 返回 | 含义 |
| --- | --- | --- |
| add，`async_mode=true`（官方默认） | HTTP 202，`queued` | 只是入队，消息还没落地 |
| add，`async_mode=false` | `accumulated` | 消息进了缓冲，还没抽取 |
| add，`async_mode=false` | `extracted` | 当场抽取完成 |
| flush | `extracted` | 把缓冲里的消息抽取了 |
| flush | `no_extraction` | 缓冲为空、内容已去重，或排队的 add 还没落地；不是错误 |

- bridge 日志和工具返回里的 `A/B` 是同一版本的「add 状态/flush 状态」，不是「v1/v2」。做法双写的返回形如 `v2 extracted/no_extraction; v1 accumulated/extracted`。
- v2 带 `async_mode=false` 时，add 当场抽取，紧接着的 flush 必然返回 `no_extraction`，属于正常。
- 含非文本内容的请求总是排队，返回 202。
- 缓冲里最新的消息要等下一次 add、flush，或在 Cloud 上闲置 2 小时才会落盘。
- 抽取成功不等于检索立刻可见。写完要用 get 或 search 回读。
- MCP 参数校验失败时，请求根本没有发出去，例如缺 `conclusion`、做法少于 3 步。看到 `-32602` 或 `isError` 要修正参数重写，不能当成已入库。

## 2. v1 与 v2

出处：官方文档、实测（2026-10-08 至 10-16）。

| 项 | v1 | v2 |
| --- | --- | --- |
| 库 | 与 v2 相互独立，同一个 key 两边都能用 | 主库 |
| 写后抽取 | 默认异步；add 后立刻 flush 会漏抽，单次写入的会话永不抽取 | 自动抽取，不丢 |
| 旧时间戳 | 约 2 个月前的时间戳返回 `accumulated`，但永不抽取 | 实测 62 天前可以入库 |
| 检索得分 | hybrid 得分全为 0，无法按相关度过滤 | 有得分，可设 `min_score` |
| 画像 | 不能改 | `/api/v2/memory/edit` 可改 |
| 做法 case | 几乎每条做法都出 case | 有提炼筛选，见第 3 节 |
| 前景 | 即将退役，官方代迁数据 | 长期使用 |

- **bridge 的写入分工**：对话入库、`memory_remember`、本地记忆同步只写 v2；只有 `memory_record_practice` 同时写 v1 和 v2。`EVEROS_READ_V1=1` 时，检索、画像、最近记忆合并读两个库。
- **消息归属**：每条消息都要带 `sender_id`。用户原话用 user_id，助手和工具消息用 agent_id。get 和 search 的 `user_id`、`agent_id` 二选一。
- **按谁查**：v2 的普通记忆（episode）只能按 `user_id` 查，按 `agent_id` 查返回 422。做法 case 和技能按 `agent_id` 查。

## 3. 做法 case 怎么生成（v2）

出处：开源实现（EverAlgo `everalgo-agent-memory` 0.4.0 的 `AgentCaseExtractor`，EverOS 2026-10-15 版本依赖的版本）、官方文档、实测。

v2 先按话题切换或时间间隔把消息切成 MemCell，再对每个 MemCell 跑做法提炼。开源实现的门槛依次是：

1. **结构**：去掉首条用户消息之前的内容；必须有用户消息和助手消息；最后一条必须是助手文字，否则跳过。
2. **没有工具调用的对话**：至少 2 条用户消息、超过 4 条消息、助手文字不少于 200 token。
3. **轮次**：工具调用少于 3 轮直接跳过。bridge 的做法里，每个步骤算一轮。
4. **长度**：裁剪后仍然过长的轨迹跳过。
5. **价值判定**：20 轮以内的轨迹交给 LLM 判断，有「探索」或「用户纠正」之一才放行，拿不准就判否。
   - **探索**：走错路后回头、非显然的诊断、意外的阻塞、权衡后的决策、可迁移的方法或坑。
   - **用户纠正**：至少 2 条用户消息，后面的消息在纠正或改向。
   - **判否**：已知流程顺利执行、查找定位、确认行为符合预期、一次性操作。
   - **判定视图**：工具输出只看开头约 80 token，参数约 120 token，助手文字约 600 token。
6. **压缩**：LLM 生成意图、做法、关键洞察和质量分；意图为空则跳过。
7. **写入**：每个 MemCell 最多一条 case，按参与的助手 sender_id 各写一份。质量分低于 0.2 的 case 不参与技能聚类。

Cloud 上的情况：
- 没有强制抽取的接口，也不返回跳过原因；再 flush 一次没有用。
- `v2 extracted/no_extraction` 只说明写入和普通记忆抽取成功，与是否生成做法 case 无关。
- 2026-10-08 至 10-15 的真实数据里，约三分之一的做法没有生成 v2 case，v1 几乎全部生成。漏掉的大多是校准、迁移、验收这类线性流程，另有一条只有 2 步。
- 2026-10-16 隔离空间实测：

| 轨迹 | 写入方式 | 次数 | 生成 case | 整次调用耗时 |
| --- | --- | --- | --- | --- |
| 有失败尝试、意外发现和用户纠正的排障 | 直接调用 core | 11 | 11 | 10 到 19 秒 |
| 同类排障 | 经 MCP 服务 | 4 | 3 | 出 case 的约 11 秒；另一次等满 30 秒没出 |
| 失败信息埋在约 350 字日志之后 | 直接调用 core | 6 | 6 | 11 到 18 秒 |
| 线性流程，但第一步被用户纠正 | 直接调用 core | 4 | 4 | 10 到 15 秒 |
| 顺利执行的三步打包流程 | 直接调用 core | 1 | 0 | 等满 150 秒，8 分钟后仍没有 |

- 合格轨迹 25 次里 24 次出了 case，case 在 v2 写完后 4 到 12 秒出现。没出的那 1 次之后是否生成，未能确认。所以 bridge 默认最多等 30 秒，提示里会说明可能仍在后台处理。
- 排障、日志埋失败、被纠正这三类都对比了新旧两种消息映射，两种映射的生成率相同。可见 Cloud 的判定不只看工具输出的开头，动作和结论里的诊断信息也会被看到。不要把 80 token 截断当成 Cloud 的硬规则。
- **推论**：v2 的做法 case 数不会追平 v1。线性流程没有 case 是设计如此，它的内容仍以普通记忆的形式可以检索。

## 4. 写出能被提炼的做法

出处：第 3 节的门槛和实测。

- 至少 3 个真实发生的步骤。只有一两步的结论用 `memory_remember`。
- 走错的路、失败的尝试单独记一步，标 `outcome=failed`；出乎意料的结果标 `outcome=unexpected`。
- `result` 第一句写这一步的发现，日志和细节放后面。
- 用户中途纠正或改向时，把原话填进那一步的 `user_correction`，不要改写。
- `why` 写当时的判断：为什么这么做、排除了什么。
- `conclusion` 写根因、解法、怎么验证、踩过的坑。
- 不要补写没发生的步骤来凑轮次，也不要重复提交同一条想刷出 case。前者是编造轨迹，后者会留下重复的普通记忆。
- bridge 写完后会回读 v2，默认最多等 30 秒，`EVEROS_CASE_WAIT` 可调，设成 0 关闭。回读结果写在工具返回里；没生成不算失败。

## 5. 读取与检索

出处：官方文档、实测。

- v2 `get` 的返回里同时有 `episodes`、`profiles`、`agent_cases`、`agent_skills` 几个列表键。要按 `memory_type` 取对应的键，不能取第一个列表。
- v2 `get` 支持 `filters`，字段有 `session_id`、`sender_id`、`timestamp`、`parent_id`、`parent_type`、`tag`。运算符有 `eq`、`ne`、`gt`、`gte`、`lt`、`lte`、`in`，可以嵌套 AND 和 OR。`user_id`、`agent_id` 不能放进 filters，否则返回 422。排序用 `sort_by=timestamp` 加 `sort_order`。
- v1 `get` 的 `memory_type` 用 `episodic_memory`、`agent_case`、`agent_skill`、`profile`。`agent_memory` 只在 search 的 `memory_types` 里有效，用在 get 上返回 422。
- 不同检索方法的得分不能直接比较。agentic 不发送 `min_score`，回退到 hybrid 后才恢复门槛。
- `top_k` 接受 `-1` 或 `1–100`。

## 6. 删除与测试空间

出处：官方文档、实测（2026-10-16）。

- `POST /api/v2/memory/delete` 是按范围的软删除。`user_id`、`agent_id`、`session_id` 至少给一个，可以组合，返回删除条数。
- 按 ID 删除用 `/api/v2/memory/delete_by_ids`，参数是 `memory_type` 和 `memory_ids[]`。
- 清空一个测试空间要删两次：按 `user_id` 删普通记忆，按 `agent_id` 删做法 case。
- 删除返回的条数不等于 case 数。按 `agent_id` 删时，v2 由 case 聚类出的技能也会算进去。要判断有没有生成 case，删除前先用 get 查 `agent_case`。
- 测试一律用独立的 user_id 和 agent_id，不要写进真实空间。测试脚本只在进程内读取 key，不要把 key 拷到别的目录。

## 7. 计费与限制

出处：官方文档、本仓库代码。

- Cloud 按 MemCell 计费，大约 10 条消息算 1 个。
- bridge 把一次 add 拆成不超过约 250 KB、400 条消息的批次；单条超过 2 万字的粘贴会被截断。
- MCP 客户端的外层超时要大于实际等待。agentic 检索默认最多 180 秒；写做法时回读默认最多 30 秒，叠加在写入耗时之上。

## 8. 排查配方

- **判断写入是否丢了**：先看版本，再看 add 状态，再用 get 或 search 回读。v2 下 `extracted/no_extraction` 是正常的。只有 v1 下 add 返回 `queued` 或 `accumulated`、flush 返回 `no_extraction`、而且回读查不到，才按丢失处理：补一次 flush，或运行 `python3 plugin/everos/hook.py --sweep`。
- **分库核对**：合并读取分不清条目来自哪个库。要分别调 v2 的 `/api/v2/memory/get` 和 v1 的 `/api/v1/memories/get`，再按 `session_id` 对齐。
- **做法 case 覆盖率**：v2 用 `memory_type=agent_case` 加 `agent_id` 查，v1 用 `memory_type=agent_case` 加 `filters.user_id` 查，两边按 `session_id` 求交集。
- **回读统计**：`state/hook.log` 里每次写做法都会记一行 `practice case <session> found`、`missing` 或 `check failed`，可以直接统计 v2 生成 case 的比例。
