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
| Claude Desktop（含 Cowork）、Cursor、Gemini CLI、TraeCode CLI | MCP | 由模型按说明调用 | 由模型按说明调用 | — | ✅ |
| Claude Code on the web（云端） | setup script + 仓库级 hook + 网络密钥 | ✅ | ✅ | ✅ | ✅ |

所有代码都是纯 Python 标准库，不用装任何依赖；任何异常都放行，不会卡住 agent。接口用 EverOS v2，v1 旧库在过渡期只读（`EVEROS_READ_V1=1`）。

## 新设备 / 新工作区

把下面这句话发给任意 agent：

> 读取 https://github.com/namelessssssssssss/everos-bridge 的 AGENTS.md，按步骤把 EverOS 记忆接到你自己和这台机器上的其他 agent。

也可以手动装：

```bash
gh auth login && gh auth setup-git            # 私有仓库需要
git clone https://github.com/namelessssssssssss/everos-bridge ~/.everos-bridge
~/.everos-bridge/install.sh                   # 首次运行会问 user_id 和 API key，写进 ~/.everos/config.env（0600）
python3 ~/.everos-bridge/plugin/everos/hook.py --doctor
```

更新时：先 `git -C ~/.everos-bridge pull`，再重跑 `install.sh`。Claude Code 直接从本地 clone 加载插件；Codex 是拷贝安装，所以要重跑。

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
install.sh                        检测本机 agent 并逐个接线（改动前备份，幂等）
plugin/                           Claude Code 与 Codex 共用的插件目录
  .claude-plugin/plugin.json      Claude Code 清单（MCP 声明在 claude-mcp.json）
  .codex-plugin/plugin.json       Codex 清单（Codex 的 MCP 由 install.sh 用绝对路径写进 config.toml）
  hooks/hooks.json                生命周期 hook
  skills/everos-memory/SKILL.md   通用 skill（Agent Skills 格式）
  everos/core.py                  核心：v2 读写（含 v1 过渡）、各宿主会话记录解析、补落盘、本地记忆同步
  everos/hook.py                  hook 入口与命令行：--doctor / --dry-run / --sweep / --sync-localmem
  everos/mcp_server.py            stdio MCP 服务：6 个 memory_* 工具
.claude-plugin/marketplace.json   Claude Code 插件市场
.agents/plugins/marketplace.json  Codex 插件市场
cloud/                            云端 setup script 与仓库级配置片段
```

## 配置

环境变量优先，其次 `$EVEROS_HOME/config.env`（默认 `~/.everos/config.env`）。完整变量表见 PROTOCOL.md §5。密钥和状态都不在仓库里，`.gitignore` 已排除。
