# EverOS Bridge

把同一份 EverOS 长期记忆接到所有 coding agent 上，换设备、上云都能直接用。规则见 [PROTOCOL.md](PROTOCOL.md)。

## 覆盖面

| Agent | 接入方式 | 自动读画像 / 首条检索 | 对话原文自动入库 | 记忆工具 |
| --- | --- | --- | --- | --- |
| Claude Code | 插件（hook + MCP + skill） | ✅ | ✅ | ✅ |
| Codex CLI / App | 插件（hook）+ MCP | ✅ | ✅ | ✅ |
| Claude Desktop（含 Cowork）、Cursor、Gemini CLI、TraeCode CLI | MCP | 由 agent 按 instructions 调用 | — | ✅ |
| Kimi Code 等其他 MCP 客户端 | 手动加 MCP 命令 | 同上 | — | ✅ |
| Claude Code on the web（云端） | setup script + 仓库级 hook + 网络密钥 | ✅ | ✅ | ✅ |

所有代码都是纯 Python 标准库，不用装任何依赖。任何异常都会放行（fail-open），不会卡住 agent。

## 新设备

```bash
gh auth login && gh auth setup-git            # 私有仓库需要
git clone https://github.com/namelessssssssssss/everos-bridge ~/.everos-bridge
~/.everos-bridge/install.sh                   # 首次运行会问 user_id 和 API key，写进 ~/.everos/config.env（0600）
```

安装脚本会自动检测本机装了哪些 agent，逐个接线：
- Claude Code、Codex：装插件；
- Codex、TraeCode CLI：注册 MCP；
- Claude Desktop、Cursor、Gemini：在配置文件里写入 MCP。

改动前都会备份原配置。新开会话后，用下面的命令自检：

```bash
python3 ~/.everos-bridge/plugin/everos/hook.py --doctor
```

更新：`git -C ~/.everos-bridge pull && ~/.everos-bridge/install.sh`。Claude Code 直接从本地 clone 加载插件，下个会话就生效；Codex 是拷贝安装，需要重跑一次安装脚本。

## 云端（Claude Code on the web）

云端会话不装插件，也不读 `~/.claude`，所以要单独接：

1. **网络与密钥**：在云端环境设置里加一个「网络密钥」，Allowed websites 填 `api.evermind.ai`，Header 填 `Authorization: Bearer <key>`。key 不进虚拟机。没有网络密钥功能时，把 `api.evermind.ai` 加进 Custom 网络白名单，并设环境变量 `EVEROS_API_KEY`。
2. **环境变量**：`EVEROS_USER_ID=<你的空间>`。仓库私有时，再加一个只读的 `GH_TOKEN`（fine-grained，只授权本仓库）。
3. **Setup script**：填 `cloud/setup.sh` 的内容，它会把本仓库装到 `/opt/everos-bridge`。
4. **个人仓库**：把 `cloud/settings.snippet.json` 合并进该仓库的 `.claude/settings.json`，把 `cloud/mcp.snippet.json` 合并进 `.mcp.json`。命令都受 `CLAUDE_CODE_REMOTE` 和文件是否存在门控，本地和其他协作者都不会触发。不要提交到团队仓库。
5. **claude.ai 网页与手机端**：把 `plugin/skills/everos-memory` 打包成 zip，上传到 claude.ai 的 Skills。云端会话也会加载它，没有 MCP 时它会改用 curl 模板。

## 目录

```
PROTOCOL.md                       记忆协议（唯一依据）
install.sh                        新设备一键接线
plugin/                           Claude Code 与 Codex 共用的插件目录
  .claude-plugin/plugin.json      Claude Code 清单
  .codex-plugin/plugin.json       Codex 清单
  hooks/hooks.json                生命周期 hook（两边都设置 CLAUDE_PLUGIN_ROOT）
  claude-mcp.json                 Claude Code 插件内置 MCP（Codex 的 MCP 由 install.sh 写进 config.toml）
  skills/everos-memory/SKILL.md   通用 skill（Agent Skills 格式）
  everos/core.py                  核心：配置、读写、会话记录解析、补 flush、本地记忆同步
  everos/hook.py                  hook 入口与命令行（--doctor / --dry-run / --sweep / --sync-localmem）
  everos/mcp_server.py            stdio MCP 服务
.claude-plugin/marketplace.json   Claude Code 插件市场
.agents/plugins/marketplace.json  Codex 插件市场
cloud/                            云端 setup script 与仓库级配置片段
```

## 配置

环境变量优先，其次 `$EVEROS_HOME/config.env`（默认 `~/.everos/config.env`）。

| 变量 | 说明 |
| --- | --- |
| `EVEROS_USER_ID` | 记忆空间，必填 |
| `EVEROS_API_KEY` | 必填；云端配了网络密钥时可以不填 |
| `EVEROS_API_BASE` | 默认 `https://api.evermind.ai` |
| `EVEROS_OWNER_ALIASES` | botmux `<sender>` 邮箱前缀命中即视为本人，逗号分隔 |
| `EVEROS_INJECT=0` / `EVEROS_WRITE=0` | 关闭读侧注入 / 写侧入库 |
| `EVEROS_HOME` | 配置与状态目录，默认 `~/.everos` |

密钥和状态都不在仓库里，`.gitignore` 已排除。
