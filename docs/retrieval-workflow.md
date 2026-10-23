# 任务中的记忆检索

0.9.0补充首轮结果驱动的多层级搜索：选有来源且影响当前未解问题的实体、旧决定或依赖，展开必要正文后做二次定向查询；新证据可继续深入，充分覆盖、无新增或重复命中时停止该分支。各层保留记忆ID、日期、线索和核实结果。服务端agentic搜索与Agent基于结果再搜是不同层，不能用前者推定外层已覆盖。规则随版本同步到[宿主原生指令](rule-sync.md)，MCP新增只读memory_rules，现共9工具。

0.8.0 起将“每条消息首次检索”扩展为任务中的历史证据工作流：首轮召回后，方案/行动前、新线索/失败、历史决定缺口和压缩恢复仍可定向补查；已有充分证据则复用。是否更主动、更有效需由行为评测证明，不能从规则存在或调用次数推导。

## 接口与数据流

```text
当前问题 + 可选任务线索 → memory_search → 带来源/日期/本机ID的摘要
                                       → memory_open → 分页记忆正文
目标/阶段/新线索 → memory_context → 后续查询与问题覆盖状态
工具阶段/异常 → 本地短提醒 → Agent 判断是否存在历史证据缺口
压缩/恢复/目标改变 → 旧覆盖失效 → 允许重新读取
```

- `memory_search(query, context?, session_id?)` 保留原参数与首轮agentic默认。query以当前要核实的问题为主；context补新线索，合并后不超过服务端500字限制。带session_id时可复用memory_context中的线索并记录实际展示的候选。
- `memory_open(memory_id, offset=0, limit=4000)` 展开本机 `em_` 标识对应的记忆快照；页长1–12000字符，返回next_offset和总长度。快照包含服务返回的episode、summary、atomic_facts或做法细节等允许字段；不承诺这些字段始终存在，也不声称是原始聊天全文。要核实时效仍须查当前事实源。
- `memory_context(session_id, goal?, stage?, clues?)` 仅更新本机临时任务上下文。stage为plan、act、new_evidence、recover；recover或目标改变清除旧覆盖。它不执行网络检索、不写EverOS。`covered_questions`仅表示候选曾展示，`coverage_level=retrieved_candidates_not_verified`明确未验证。

检索工具声明只读；memory_context声明为本机状态写操作；长期记忆写工具保留写属性，画像编辑标记可能删除。工具提示不是权限授权。宿主对工具的实际审批仍按自身策略执行。

## 覆盖与快照

旧账本的ids在首次迁移时保留兼容；迁移后按问题指纹与内容版本去重。新问题可重新展示同一条记忆；正文改变产生新标识；压缩、恢复清除覆盖而保留快照。覆盖最多24个问题，只标记真正展示的候选，不从输出行数推断。

快照保存在 `$EVEROS_HOME/state/reads/`，标识绑定后端、owner和agent身份及来源/内容，跨身份拒绝读取。文件以0600原子落盘，不缓存embedding、任意传输元数据或API密钥；这不表示正文里的历史内容已自动脱敏。快照只在本机，其他设备重新检索。它们与本地agent-memory及Codex内置记忆分别按各自边界管理。

## 宿主范围

| 宿主 | 初始召回 | 任务中补查与展开 | 自动工具阶段提醒 | 压缩恢复 |
| --- | --- | --- | --- | --- |
| Claude Code / Codex | UserPromptSubmit | 共享规则＋8个MCP工具 | PreToolUse方案/修改前，PostToolUse失败后；均无网络、失败放行 | PreCompact失效，SessionStart compact/resume恢复规则 |
| TraeX | 既有UserPromptSubmit | 共享规则＋MCP | 0.210.1起接入PreToolUse/PostToolUse本地提醒；旧版保留规则补查 | 既有PreCompact/PostCompact |
| Kimi Code / Kimi Work | 既有提问hook | MCP指令；桌面端同步skill | 不添加未核实事件 | 既有PreCompact或显式memory_context recover |
| MiMoCode / Desktop | 既有Plugin chat.message | 共享规则＋MCP | tool.execute.before提示进入下次模型上下文；after提示追加工具输出 | experimental.session.compacting本机失效与规则恢复 |
| 仅MCP客户端 | Agent主动首轮agentic | MCP指令与工具 | 无宿主hook时由Agent判断 | 显式recover |

工具提醒跳过memory工具，按动作/异常去重，只给证据缺口检查提示，不自动检索、不批准或阻止操作。错误识别是有限的文本启发式，不等同于证明工具技术失败或识别所有新实体；新实体的语义判断仍由Agent承担。

更新hook后须按宿主提示审阅信任，旧聊天与常驻MCP不能被默认为已热更新。

## 可复现隔离评测

`python3 scripts/eval/run_retrieval.py --output-dir <仓外绝对路径>` 使用当前Codex CLI默认模型，比较：

1. `auto`：主干旧实现＋初始摘要；
2. `policy`：相同旧实现＋主动补查规则；
3. `workflow`：候选实现＋规则、展开工具与阶段提示；
4. `proactive`：候选实现，去掉初始摘要，检验Agent主动检索。

初始摘要只在固定合成案例中提供；这是对照实验中的自动供给模拟，不是真实生命周期hook质量评估。四组底层合成记忆相同，现场通过只读inspect_runtime揭示，详细条件放在摘要之外；另有无需历史细节的控制题。候选代码复制冻结，记录逐文件SHA256，基线固定commit。不同组不可用不同语义答案源。

fixture MCP不调用EverOS，不写记忆；所有用户/场景均为合成数据。实际答卷、调用、错误、代码指纹、判分与哈希保存在仓外。脚本使用已有Codex认证，不把凭据内容复制进仓库；运行临时认证入口不得打包分享。原始失败试跑保留，技术失败不计为质量零分。

`python3 scripts/eval/judge_retrieval.py <同一输出路径>` 使用默认Codex模型按明确判据做语义判分；脚本只检查结果身份、结构与调用轨迹，并生成manifest。判分不得以工具调用次数奖励；必要补查、原文展开、历史适用范围、任务结论与时间开销分别看。少量合成题仅能说明代表性路径是否跑通，不证明跨模型、跨宿主或长期任务的普遍收益。

实际hook进程、关闭注入、恢复失效、跨身份拒绝与分页等由离线回归单独覆盖；真实EverOS只读验证用于确认search→open和本地覆盖链路，不向真实空间写入评测案例。

本次代表性验证结果见[2026-10-22验证记录](retrieval-workflow-eval-2026-10-22.md)。

0.8.1补齐Cursor/Gemini/Claude Desktop首次配置及显式MCP状态目录绑定，详见[跨harness验收](harness-activation-2026-10-22.md)。
