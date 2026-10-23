# TraeX L3 / L4 接入与验证

Bridge 0.5.0 支持 TraeX 0.209.1 及兼容版本。2026-10-10 在 macOS arm64 上验证，
同时保留主干已有的 MiMoCode 和 Kimi 接入能力。

## 接入契约

| 项目 | 实现 |
| --- | --- |
| MCP / 规则 | `$TRAE_HOME/traecli.toml` / `$TRAE_HOME/AGENTS.md`，默认 `~/.trae` |
| 用户级 hook | `$TRAECLI_HOME/hooks.json`，默认 `$TRAE_HOME/cli/hooks.json` |
| L3 | SessionStart 读画像，UserPromptSubmit 检索；输出 `hookSpecificOutput.additionalContext` |
| 压缩 | PostCompact 标记下轮补读画像并清除已注入 ID；下轮画像和召回共享 18 秒预算 |
| L4 | Stop 后台增量采集；SessionEnd / PreCompact 补写并 flush |
| 会话定位 | 优先 `transcript_path`；为空时按 session_id 只读查 `state_5.sqlite`，再精确匹配 rollout 文件名 |
| 新格式 / 旧格式 | `history_mutation` version 1 的 append items / `response_item` message |
| 归属 | 用户为 owner，助手为配置的 agent_id、sender_name=trae；session 用 `tx_` 前缀和完整 UUID，避免 UUIDv7 的时间前缀碰撞 |

保留用户和助手的文本。现代格式的 `content_item_kinds` 区分用户输入与宿主注入，
用户主动提供的 `# AGENTS.md` 等文本不会因前缀相似而被丢掉；旧格式缺少元数据时按
已知脚手架前缀过滤。工具事件、UI 镜像、reasoning、图片二进制和替换/压缩快照不作为新对话采集。
单条文本仍遵守协议的 20000 字上限。

写入继续使用会话文件锁、完整行边界和成功后提交偏移。重复 Stop / SessionEnd 只写一次，
失败保留偏移和 pending。`EVEROS_WRITE=0` 禁止采集，`EVEROS_INJECT=0` 禁止注入；
hook 异常记日志后放行主会话。

主目录工作区可能将同一个 hooks.json 同时作为 user 和 project 来源加载。
读侧按 session 加锁，以 turn_id + prompt 哈希去重；同 source 的 SessionStart 两秒内去重。
不同提问或后续 turn 仍会检索，写侧依靠锁和偏移防止重复入库。

## 安装与信任

```bash
./install.sh --only trae --dry-run
./install.sh --only trae
python3 plugin/everos/hook.py --doctor
```

安装器先备份，只合并 EverOS handler，保留其他 hook 和信任记录，重复运行不追加重复项。
入口优先级为 traecli、traex、coco。识别到旧 Coco 时保留 MCP + 规则并明确提示。

新会话在 `/hooks` 审阅并信任六个 EverOS 定义。安装器本身不自动信任。
已获授权并审阅定义的自动化调用方，可用 app-server `hooks/list` 获取 key/currentHash，
通过 config API 只更新对应信任记录，再回读核验。`keyPath` 按点分割，应对 `hooks.state`
upsert 完整键对象，并验证其他记录不变；不需要关闭宿主的信任机制。

## 可复现验证

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
python3 scripts/validate_trae.py --out /tmp/everos-traex-validation
python3 scripts/validate_trae.py --home-workspace --out /tmp/everos-traex-home-validation
```

验证脚本使用全新的三个 home、标准库 HTTP 服务和固定 SSE 模型回复，调用真实 TraeX CLI。
不读取真实凭据、不修改本机宿主配置、不写云端记忆。脚本使用正常定义信任，检查画像/召回
到达模型请求，以及采集的正文、角色、归属和唯一性。报告和隔离运行记录保存在指定的新目录。

2026-10-10 已验证：

- 离线回归：135 项通过，包含主干 MiMoCode、Kimi 和其他宿主的既有用例。
- 普通工作区：SessionStart / UserPromptSubmit / Stop / SessionEnd 均触发；模型收到画像与召回，自动采集 user + assistant。
- 主目录工作区：四类事件各触发两次；模型仍只收到一份画像和召回，只有一次 add、一次 flush。
- 本机已安装 hook：六个定义 enabled + trusted；保留其他信任记录，doctor 各项 OK。
- 真实 Cloud 初次验收：Stop 写入 2 条消息并返回 extracted；SessionEnd 返回 no_extraction，pending=false。回读 episode `6aca30c10d4f95c677fed139`，主题「TraeX L3/L4 自动采集验收与 hook 链路验证」。
- 整合主干后的 0.5.0 再验收：episode `6aca337488b86894f92bddb0`，session `tx_20261010_workspace_01a125d8-e610-7ea2-a052-776210eeb8c1`，同样自动写入 user/assistant 两条并成功回读；使用完整 UUID 隔离会话。

本机 TraeX 的 MCP / hooks 当前指向独立检出目录 `~/.everos-bridge-traex`，六个定义均已信任；
此路径避免其他宿主更新 `~/.everos-bridge` 时切换掉待合并实现。MR 合并后可从主干重新运行安装器迁回。

Cloud 验收单独运行，沿用本机已安装且信任的 hook，用环境变量隔离账本，以本地固定模型
避免依赖远端模型服务。它是真实写入，与仓库提供的离线验证脚本分开。

验证边界：真实宿主测试覆盖新会话正常读写和重复加载；PreCompact / PostCompact 状态恢复、
断网放行、重试、空 transcript_path、半行写入、并发锁与 UUIDv7 隔离由回归用例验证。
旧 Coco 的其他格式尚未适配；TraeX 将来改变 history_mutation schema 时需重新验收。

## 0.8.1 补充验收

TraeX 0.210.1本机原生进程现已验证：八个hook（原六类加PreToolUse/PostToolUse）、本地固定模型网关下经Code Mode真实调用memory_search/memory_open、展开正文到达模型、采集仅保留用户与助手正文。不是仅给MCP clientInfo换名。

MCP启动需在[mcp_servers.everos.env]显式绑定EVEROS_HOME；验证脚本同时显式配置本地API与凭据占位符、禁用非loopback Python API请求。此前失败隔离试跑不作为通过证据。当前默认模型服务是否可用与bridge协议验收分别记录。上述旧版本预算、路径和六hook信任状态仅保留为历史；最新配置与能力见PROTOCOL和跨harness验收。
