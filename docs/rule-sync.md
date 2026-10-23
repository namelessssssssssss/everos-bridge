# 原生指令同步与规则增量

0.9.0起，接入Agent在首次安装和每次更新时都要读新规则、理解相对旧版的增删改、同步宿主原生指令并回读。`install.sh` 自动完成可确定的文件同步；`install.sh --rules-only` 只同步规则，不安装插件、不调用记忆API，不要求API密钥。云端可显式用 `--rules-only --rules-host claude`，无需猜测尚未启动的CLI。其它支持的文件宿主也可重复指定 `--rules-host`。

## 原生入口

| 宿主 | 接入/更新的默认规则入口 |
| --- | --- |
| Codex | `$CODEX_HOME/AGENTS.md`，默认 `~/.codex/AGENTS.md`；存在非空 `AGENTS.override.md` 时更新实际优先加载的该文件 |
| Claude Code，包括云端 | `$CLAUDE_CONFIG_DIR/CLAUDE.md`，默认 `~/.claude/CLAUDE.md` |
| TraeX / Trae CLI / Coco | `$TRAE_HOME/AGENTS.md` |
| Kimi Code | `$KIMI_CODE_HOME/AGENTS.md` |
| Kimi Work桌面端 | 已配置的运行时home中的 `AGENTS.md`，保留原有共享skill同步 |
| Gemini CLI | `~/.gemini/GEMINI.md`；已有配置但当前无CLI也同步 |
| Cursor | `~/.cursor/rules/everos-bridge.mdc`，`alwaysApply: true`；全局skill继续同步 |
| MiMoCode / MiMo Desktop | 既有 `instructions` 引用源仓 `plugin/rules/everos-rules.md`，更新源文件即更新该指令来源；安装报告回读引用是否存在 |
| Claude Desktop / Cowork / 其它仅MCP入口 | MCP声明当前版本并提供 `memory_rules`。不猜测未公开的全局记忆文件；若宿主提供原生记忆/指令工具，接入Agent在本次授权内保存规则摘要和版本，否则明确报告原生持久化缺口 |

文件入口只管理 `<!-- everos-bridge:begin -->` 到 `<!-- everos-bridge:end -->`，含规则包SHA256；用户其他指令不进入规则快照、不被改写。更新删除过期块内规则。损坏/重复边界、同名非Bridge专用Cursor文件、符号链接会保留原文件并报具体问题，不将失败记成成功。写入前备份、原子替换，随后核对正文。宿主自动生成的episodic/auto memory、业务仓AGENTS以及其它记忆系统的数据均不在此写入范围。

## 首次、更新和再次运行

- 可执行规则包由简版 `plugin/rules/everos-rules.md` 与详细skill组成，二者都纳入内容版本；规范来源仍是PROTOCOL，维护时须同步。
- `$EVEROS_HOME/state/rules/current.json` 保存上一轮实际同步前后的规则差异；`targets.json` 保存本次目标路径与文件/引用回读结果，均0600。没有真实增量的再次安装保留上次差异，避免Agent尚未读到就丢失。
- 从无版本快照的旧安装迁移时，可从一致的既有Bridge管理块恢复简版规则基线；旧skill没有可信快照时标明该基线缺失，不能把全部内容宣称为新规则。没有任何可信基线时全量复核。不会从用户的其它指令或远端历史记忆拼造旧规则。
- `memory_rules(section="changes"|"rules"|"skill", offset=0, limit=6000)` 分页返回当前规则、前版标识、基线状态和文件接线证据；`next_offset`非空继续。工具不联网、不写记忆、不要求凭据；旧插件缓存读取自身规则，不能拿新版安装报告冒充自身已更新。
- 安装成功、Agent已读、活动会话已重载是不同证据。接入Agent须先对照差异、读新规则并回读原生入口，再向用户说明完成范围。旧MCP/插件目录需按宿主重连或新开会话；没有工具的宿主不假装已经写入原生记忆。

原生文件与加载机制依据：[Codex指令加载](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide)、[Claude指令与自动记忆](https://code.claude.com/docs/en/memory)、[Kimi全局指令](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/agents.html)、[Cursor本机用户规则](https://prod.cursor.com/help/customization/rules)。文件接线核验仍不等于每个GUI或未提供云端实例的现场验收。

## 2026-10-23验证

221项离线回归通过，覆盖七类文件入口、首次接入/更新/重复安装、用户内容与备份、Codex override、规则差异与旧基线缺失、分页无凭据读取和旧缓存版本隔离。技能校验及Python/shell语法检查通过。

真实Codex使用隔离用户目录，确实加载安装器生成的全局AGENTS指令，并复述仅该文件中的合成标记。多层搜索的合成场景中，模型先检索/展开决定记录，再用正文新出现的LQ-73关联检索/展开另一条租约记忆，正确给出原因及v1范围，并区分v2；该标识与skill示例不同，无关UI主题未扩散。纯计算对照题无额外工具调用。原始答卷、查询和代码指纹保留仓外，合成案例不写真实EverOS。这些是代表性路径验证，不是跨模型统计结论，也不冒充其它GUI/云端实例的现场验证。
