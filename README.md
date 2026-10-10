# EverOS Bridge

把同一份 EverOS 长期记忆接到所有 coding agent 上，换设备、换工作区、上云都能直接用。

- **Agent 从这里开始**：[AGENTS.md](AGENTS.md)。新工作区里让 agent 读它，就能重建默认的读写流程。
- **规则**：[PROTOCOL.md](PROTOCOL.md)。
- **方法论**：[docs/methodology.md](docs/methodology.md)。
- **新 Agent 接入清单**：[docs/agent-onboarding.md](docs/agent-onboarding.md)。

## 覆盖面

| Agent | 接入方式 | 会话开始读画像 | 每条提问检索 | 对话原文入库 | 记忆工具 |
| --- | --- | --- | --- | --- | --- |
| Claude Code | 插件（hook + MCP + skill） | ✅ | ✅ | ✅ | ✅ |
| Codex CLI / App | 插件（hook）+ MCP | ✅ | ✅ | ✅ | ✅ |
| Kimi Code | config.toml hook + MCP | ✅（首条提问时注入） | ✅ | ✅ | ✅ |
| Kimi Work 桌面端（Kimi.app） | runtime config.toml hook + MCP | ✅（首条提问时注入） | ✅ | ✅ | ✅ |
| TraeX ≥ 0.209.1（traecli / traex / coco 别名） | 用户级 hook + MCP + 全局规则 | ✅ | ✅ | ✅ | ✅ |
| 旧 Coco、Gemini CLI | MCP + 全局规则文件 | 规则要求模型自己调用 | 同左 | — | ✅ |
| Claude Desktop（含 Cowork）、Cursor | MCP | 由模型按说明调用 | 由模型按说明调用 | — | ✅ |
| Claude Code on the web（云端） | setup script + 仓库级 hook + 网络密钥 | ✅ | ✅ | ✅ | ✅ |

Python 代码只用标准库，不用安装依赖；运行期 hook 失败放行，MCP 和安装器会明确报告失败。接口用 EverOS v2，v1 旧库在过渡期合并读取（`EVEROS_READ_V1=1`），做法轨迹仍按协议双写。

## 按次深检索

`memory_search` 默认 `method="hybrid"`。隐含描述或复杂历史问题未被普通召回覆盖时，可显式选择 `agentic`：

```json
{"query":"写入成功后紧接着抽取却说没有内容，那次问题的根因是什么？","kind":"episodes","top_k":5,"method":"agentic"}
```

`kind` 仍支持 `all` / `episodes` / `practices`，`top_k` 支持 `-1` 或 `1–100`。agentic 的共同预算为 45 秒，包含按轨道 hybrid 回退；输出会说明失败、超时或空结果时的回退。全部轨道技术失败时工具报错，不伪装成没有记忆。v1 兼容请求始终用 hybrid，v1-only 会提示降级。

agentic 不套用 hybrid 的得分门槛，回退结果恢复对应门槛。每次输出仍限制在 4000 字内；它是按次补查选项，hook 仍用 hybrid 和原预算。插件更新后需重连 MCP 或新开聊天加载新 schema。质量对比见 [调研报告](docs/retrieval-study-2026-10-09.md)。

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
docs/agent-onboarding.md          新 Agent 接入：自查清单、实现步骤、验证清单、已接入宿主速查
install.sh                        检测本机 agent 并逐个接线（改动前备份，幂等；支持非交互）
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
  everos/hook.py                  hook 入口与命令行：--doctor / --dry-run / --sweep / --sync-localmem
  everos/mcp_server.py            stdio MCP 服务：6 个 memory_* 工具
.claude-plugin/marketplace.json   Claude Code 插件市场
.agents/plugins/marketplace.json  Codex 插件市场
cloud/                            云端 setup script 与仓库级配置片段
```

## 配置

环境变量优先，其次 `$EVEROS_HOME/config.env`（默认 `~/.everos/config.env`）。完整变量表见 PROTOCOL.md §5。密钥和状态都不在仓库里，`.gitignore` 已排除。
