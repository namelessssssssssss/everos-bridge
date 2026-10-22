# EverOS Bridge

把同一份 EverOS 长期记忆接到所有 coding agent 上，换设备、换工作区、上云都能直接用。

- **Agent 从这里开始**：[AGENTS.md](AGENTS.md)。新工作区里让 agent 读它，就能重建默认的读写流程。
- **规则**：[PROTOCOL.md](PROTOCOL.md)。
- **方法论**：[docs/methodology.md](docs/methodology.md)。
- **EverOS 诀窍、局限与坑点**：[docs/everos-notes.md](docs/everos-notes.md)。
- **新 Agent 接入清单**：[docs/agent-onboarding.md](docs/agent-onboarding.md)。

## 覆盖面

| Agent | 接入方式 | 会话开始读画像 | 每条提问检索 | 对话原文入库 | 记忆工具 |
| --- | --- | --- | --- | --- | --- |
| Claude Code | 插件（hook + MCP + skill） | ✅ | ✅ | ✅ | ✅ |
| Codex CLI / App | 插件（hook）+ MCP | ✅ | ✅ | ✅ | ✅ |
| Kimi Code | config.toml hook + MCP | ✅（首条提问时注入） | ✅ | ✅ | ✅ |
| Kimi Work 桌面端（Kimi.app） | runtime config.toml hook + MCP | ✅（首条提问时注入） | ✅ | ✅ | ✅ |
| MiMoCode / MiMo Desktop | Plugin API（L3/L4）+ MCP + instructions | ✅（首条提问注入画像） | ✅ | ✅ | ✅ |
| TraeX ≥ 0.209.1（traecli / traex / coco 别名） | 用户级 hook + MCP + 全局规则 | ✅ | ✅ | ✅ | ✅ |
| 旧 Coco、Gemini CLI | MCP + 全局规则文件 | 规则要求模型自己调用 | 同左 | — | ✅ |
| Claude Desktop（含 Cowork）、Cursor | MCP | 由模型按说明调用 | 由模型按说明调用 | — | ✅ |
| Claude Code on the web（云端） | setup script + 仓库级 hook + 网络密钥 | ✅ | ✅ | ✅ | ✅ |

Python 代码只用标准库，不用安装依赖；运行期 hook 失败放行，MCP 和安装器会明确报告失败。接口用 EverOS v2，v1 旧库在过渡期合并读取（`EVEROS_READ_V1=1`），做法轨迹仍按协议双写。

所有宿主的记忆工具都由同一个 `plugin/everos/mcp_server.py` 提供，工具说明和写入校验对每个宿主一致。`memory_record_practice` 至少要 3 个真实步骤，写完会回读 v2 是否生成做法 case，并在返回里说明，等待上限由 `EVEROS_CASE_WAIT` 控制。

## 手动检索 CLI

不打开 agent 也能在终端检索。完整安装会创建 `~/.local/bin/everos`；只需要 CLI 时运行：

```bash
~/.everos-bridge/install.sh --cli-only
everos search "之前遇到的记忆抽取问题怎么解决？"
everos search "排障做法" --kind practices --top-k 10
everos search "复杂的历史问题" --method agentic
everos search "复杂的历史问题" --method agentic --timeout 240
```

`~/.local/bin` 需在 PATH 中；也可以直接运行 `~/.everos-bridge/bin/everos search "问题"`。入口跟随仓库，不依赖当前目录。`--cli-only --dry-run` 只预览安装；`--cli-only` 不修改凭据或宿主配置，也不调用 API。已有其他文件占用命令路径时会报错并保留原文件。

配置仍从环境变量和 `$EVEROS_HOME/config.env`（默认 `~/.everos/config.env`）读取。检索命令不会写入记忆或更新会话去重账本。

- `--kind all|episodes|practices`：默认 `all`，检索事件和做法。
- `--top-k N`：默认 `5`；接受 `-1` 或 `1–100`，是每类记忆的上限，合并后可能超过 N。
- `--method hybrid|agentic`：默认 `hybrid`；复用 MCP 的 v1/v2 合并、agentic 默认 180 秒共同预算与回退。
- `--timeout SECONDS`：按次设置总预算（正有限秒数，含回退），优先于配置。未设置时 agentic 采用 `EVEROS_AGENTIC_TIMEOUT`，hybrid 沿用原预算。
- 问题须为 1–500 字。结果采用 MCP 的文本摘要格式，单条最多 260 字、整体约 4000 字；回退或部分失败会明确提示。

退出码：`0` 表示检索完成（包含未命中或部分轨道成功），`1` 表示配置缺失或检索全部失败，`2` 表示参数错误，`130` 表示用户取消。`everos --help` 和 `everos search --help` 无需配置，不调用 API。

## 每条消息首次深检索

自动 hook 和 `memory_search` 默认使用 `agentic`：每条用户消息首次检索直接深查，同一会话的后续消息也一样。本轮同一问题已有充分证据时复用；新问题或缺口仍应补查；后续补查可显式选择 hybrid。独立只读 CLI 保留默认 hybrid。示例：

```json
{"query":"写入成功后紧接着抽取却说没有内容，那次问题的根因是什么？","kind":"episodes","top_k":5,"method":"agentic"}
```

`kind` 仍支持 `all` / `episodes` / `practices`，`top_k` 支持 `-1` 或 `1–100`。agentic 的默认共同预算为 180 秒，包含按轨道 hybrid 回退；输出会说明失败、超时或空结果时的回退。全部轨道技术失败时工具报错，不伪装成没有记忆。v1 兼容请求始终用 hybrid，v1-only 会提示降级。

长期调整可在 `~/.everos/config.env` 设置 `EVEROS_AGENTIC_TIMEOUT=240`，或用同名环境变量覆盖。MCP 也支持按次传 `timeout`（秒）：

```json
{"query":"复杂的历史问题","method":"agentic","timeout":240}
```

优先级是按次 `timeout` / CLI `--timeout` > 环境变量 > config.env > 默认 180 秒。必须为正有限数值；非法配置明确报错。默认先给 agentic 约 168 秒，给回退预留至多 12 秒，整次调用仍受同一个 deadline 约束。

MCP 客户端的外层超时需大于实际总预算。安装器会为 Codex 设置 `tool_timeout_sec`，为 MiMo 设置毫秒 `timeout`，至少达到配置预算 + 10 秒（默认 190 秒），并保留更大的现值。提高长期配置后重跑 `install.sh` 同步外层限制；按次传更大的 timeout 时也需确保宿主允许等待。其他宿主需按各自配置设置外层超时。

agentic 不套用 hybrid 的得分门槛，回退结果恢复对应门槛。每次输出仍限制在 4000 字内；自动 hook 使用相同深检索与回退规则，默认总预算180秒、外层190秒；旧 EVEROS_RECALL_TIMEOUT 不再限制提问检索。Kimi / TraeX / MiMo 外层由安装器按配置预算加10秒设置；Claude Code / Codex 插件和云端静态 hook 默认190秒，预算超过180秒时需同步提高 UserPromptSubmit timeout 并重新审阅、信任。插件更新后需重连 MCP 或新开聊天加载新 schema。质量对比见 [调研报告](docs/retrieval-study-2026-10-09.md)。

## 任务中主动检索

每条消息首轮agentic后，遇到新错误码、历史决定缺口、行动前约束或压缩恢复应继续补查。已有注入只覆盖对应问题。MCP的memory_search可带context与session_id；memory_context保存本机目标、阶段和线索；memory_open按搜索返回的em_ ID分段展开。

CLI同样支持 `everos search "当前历史问题" --context "新线索" --session-id <sid>` 和 `everos open <em_id> --offset 0 --limit 4000`。结果标注来源、日期和截断；快照不是原始聊天全文或实时事实。Claude Code/Codex工具事件只做本地阶段提醒，不为每次工具调用发起网络检索。能力与评测见[检索工作流](docs/retrieval-workflow.md)。

## 新设备 / 新工作区

把下面这句话发给任意 agent：

> 用 gh 把私有仓库 namelessssssssssss/everos-bridge 拉到 ~/.everos-bridge，读里面的 AGENTS.md，按步骤把 EverOS 记忆接到你自己和这台机器上的其他 agent。

也可以手动装：

```bash
gh auth login && gh auth setup-git            # 私有仓库需要
git clone https://github.com/namelessssssssssss/everos-bridge ~/.everos-bridge
~/.everos-bridge/install.sh                   # 首次运行会问 user_id 和 API key，写进 ~/.everos/config.env（0600）
python3 ~/.everos-bridge/plugin/everos/hook.py --doctor
```

更新时：先 `git -C ~/.everos-bridge pull`，再重跑 `install.sh`。Claude Code 直接从本地 clone 加载插件；Codex 是拷贝安装，所以要重跑。Kimi Work 桌面端的 `config.toml` 会被 App 升级重写，升级后同样重跑 `install.sh` 恢复。

仓库不强制放在 `~/.everos-bridge`：`install.sh` 以自身所在路径为准，从任何 clone 位置运行都可以；它会把 `PROTOCOL.md` 软链到 `~/.everos/PROTOCOL.md` 作为稳定入口，并同步 Kimi Work 桌面端的 daimon skill（`skills/everos-memory`）。

`kimi` 不在 PATH 时，安装脚本会自动探测默认安装位置 `~/.kimi-code/bin/kimi`；`~/.kimi-code` 存在但找不到可执行的 kimi 时会打印明确的跳过提示，不再静默漏装。

Codex 插件安装或更新后，还需在新会话中审阅并信任 hook 的当前定义，自动注入和采集才会执行。启用插件与 MCP 可连接本身不能证明 hook 已生效。

TraeX 可只刷新当前宿主：`./install.sh --only trae`（也支持 `--dry-run`）。配置和规则写到
`$TRAE_HOME`（默认 `~/.trae`），六个 hook 写到 `$TRAECLI_HOME/hooks.json`（默认
`~/.trae/cli/hooks.json`）。新会话在 `/hooks` 审阅并信任 EverOS 的当前定义；安装器不自动信任。
自动采集保存用户和助手的文本，排除环境提示、注入记忆、工具镜像及 reasoning；不上传图片二进制。
压缩后在下一条提问补读画像。实现与验证细节见 [TraeX 接入](docs/traex-integration.md)。

`install.sh --dry-run` 只展示接线动作，不改配置、不执行联网自检。正常安装先备份配置再修改；子命令或自检失败会返回非零。需要暂停所有入库时设置 `EVEROS_WRITE=0`，它同时约束自动采集、MCP 写工具、画像编辑与 flush。

## 本地回归验证

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
# 可选：真实 TraeX 进程 + 本地固定模型/记忆 API，不读取真实凭据
python3 scripts/validate_trae.py --out /tmp/everos-traex-validation
```

测试使用临时配置、模拟 API 和宿主 CLI，不读取真实凭据，不修改本机 agent 配置，也不写入远端记忆。真实接入仍按 [Agent 接入清单](docs/agent-onboarding.md) 核验新会话注入、MCP 调用和写入回读；账号或 provider 缺少凭据时只说明限制，不能算通过。

## 云端（Claude Code on the web）

云端会话不装插件，也不读 `~/.claude`，要单独接：

1. **网络与密钥**：在云端环境设置里加一个网络密钥，Allowed websites 填 `api.evermind.ai`，Header 填 `Authorization: Bearer <key>`，这样 key 不进虚拟机。没有网络密钥功能时，把 `api.evermind.ai` 加进 Custom 网络白名单，并设环境变量 `EVEROS_API_KEY`。
2. **环境变量**：`EVEROS_USER_ID=<你的空间>`。过渡期再加 `EVEROS_READ_V1=1`。仓库私有时，加一个只读的 `GH_TOKEN`（fine-grained，只授权本仓库）。
3. **Setup script**：填 `cloud/setup.sh` 的内容，它会把本仓库装到 `/opt/everos-bridge`。
4. **个人仓库**：把 `cloud/settings.snippet.json` 合并进该仓库的 `.claude/settings.json`，把 `cloud/mcp.snippet.json` 合并进 `.mcp.json`。这些命令受 `CLAUDE_CODE_REMOTE` 和文件是否存在门控，本地和其他协作者都不会触发。不要提交到团队仓库。
5. **claude.ai 网页、手机端、Cowork**：把 `plugin/skills/everos-memory` 打包成 zip，上传到 claude.ai 的 Skills。

## 目录

```
AGENTS.md / CLAUDE.md             Agent 入口（CLAUDE.md 引用 AGENTS.md）
PROTOCOL.md                       读写规则（唯一依据）
docs/methodology.md               方法论：原则、决策记录、已知坑、演进路线
docs/everos-notes.md              EverOS 服务本身的诀窍、局限与坑点：状态、v1/v2 差异、做法 case 门槛、接口与实测
docs/agent-onboarding.md          新 Agent 接入：自查清单、实现步骤、验证清单、已接入宿主速查
install.sh                        检测本机 agent 并逐个接线（改动前备份，幂等；支持非交互）；同步 Kimi Work 桌面端 daimon skill，并把 PROTOCOL.md 软链进 ~/.everos/
bin/everos                        手动检索入口；安装到 ~/.local/bin/everos，支持 install.sh --cli-only
scripts/install_config.py          安装配置的备份、校验与局部迁移
tests/                            离线回归测试
plugin/                           Claude Code 与 Codex 共用的插件目录
  .claude-plugin/plugin.json      Claude Code 清单（MCP 声明在 claude-mcp.json）
  .codex-plugin/plugin.json       Codex 清单（Codex 的 MCP 由 install.sh 用绝对路径写进 config.toml）
  hooks/hooks.json                生命周期 hook
  skills/everos-memory/SKILL.md   通用 skill（Agent Skills 格式）
  rules/everos-rules.md           写进只有 MCP 的 agent 全局指令文件的规则块
  everos/core.py                  核心：v2 读写（含 v1 过渡）、各宿主会话记录解析、补落盘、本地记忆同步
  everos/trae.py                  TraeX 会话定位、history_mutation / response_item 文本投影
  everos/hook.py                  hook 入口与命令行：--doctor / --dry-run / --mimo-recall / --mimo-write / --sweep / --sync-localmem
  everos/mcp_server.py            stdio MCP 服务：8 个 memory_* 工具
  everos/cli.py                   手动检索 CLI：search（与 MCP 共用配置和检索核心）
  mimo/everos.mjs                 MiMoCode Plugin API：L3 注入 + L4 trajectory 入库
.claude-plugin/marketplace.json   Claude Code 插件市场
.agents/plugins/marketplace.json  Codex 插件市场
cloud/                            云端 setup script 与仓库级配置片段
```

## MiMoCode / MiMo Desktop

不走 hooks.json，用 `@mimo-ai/plugin` 的生命周期钩子：`chat.message` / `experimental.chat.system.transform` 做 L3 注入，`session.post` 把 `trajectory` 交给 `hook.py --mimo-write` 做 L4。安装器会合并 `~/.config/mimocode/mimocode.jsonc` 的 `mcp` / `instructions` / `plugin`。MCP 的毫秒 `timeout` 默认至少 190000，需大于实际 agentic 总预算，否则深检索会被宿主提前截断。配置与插件变更在新会话生效。

## 配置

环境变量优先，其次 `$EVEROS_HOME/config.env`（默认 `~/.everos/config.env`）。完整变量表见 PROTOCOL.md §5。密钥和状态都不在仓库里，`.gitignore` 已排除。
